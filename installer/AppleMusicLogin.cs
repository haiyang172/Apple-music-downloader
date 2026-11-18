using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

internal static class AppleMusicLogin
{
    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            var options = new Dictionary<string, string>(StringComparer.Ordinal);
            bool clear = false;
            for (int index = 0; index < args.Length; index++)
            {
                if (args[index] == "--clear-profile" && !clear)
                {
                    clear = true;
                    continue;
                }
                string name = args[index];
                if ((name != "--pipe" && name != "--profile" && name != "--region")
                    || options.ContainsKey(name) || ++index >= args.Length)
                    return 2;
                options.Add(name, args[index]);
            }
            if (options.Count != 3
                || !Regex.IsMatch(options["--pipe"], @"\Aamdl-apple-login-[A-Za-z0-9_-]{8,96}\z")
                || !Regex.IsMatch(options["--region"], @"\A[a-z]{2}\z")
                || !Path.IsPathRooted(options["--profile"])
                || options["--profile"].StartsWith(@"\\", StringComparison.Ordinal))
                return 2;

            string profile = Path.GetFullPath(options["--profile"]);
            Directory.CreateDirectory(profile);
            SecurityIdentifier user = WindowsIdentity.GetCurrent().User;
            var security = new PipeSecurity();
            security.SetOwner(user);
            security.SetAccessRuleProtection(true, false);
            security.AddAccessRule(new PipeAccessRule(user, PipeAccessRights.FullControl, AccessControlType.Allow));
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            using (var pipe = new NamedPipeServerStream(options["--pipe"], PipeDirection.Out, 1,
                PipeTransmissionMode.Byte, PipeOptions.Asynchronous, 4096, 16384, security))
            using (var window = new LoginWindow(pipe, profile, options["--region"], clear))
            {
                Application.Run(window);
                return window.Outcome;
            }
        }
        catch
        {
            return 1;
        }
    }

    private sealed class LoginWindow : Form
    {
        [DllImport("user32.dll")]
        private static extern uint GetDpiForWindow(IntPtr window);

        private static readonly HashSet<string> AllowedHosts = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "music.apple.com", "auth.music.apple.com", "authorize.music.apple.com",
            "amsui.apple.com", "idmsa.apple.com", "appleid.apple.com", "account.apple.com",
            "buy.music.apple.com", "buy.itunes.apple.com"
        };
        private const string TokenScript =
            "(function(){if(location.origin!=='https://music.apple.com')return null;"
            + "try{var m=window.MusicKit&&window.MusicKit.getInstance();"
            + "return m&&m.isAuthorized===true&&typeof m.musicUserToken==='string'?m.musicUserToken:null;"
            + "}catch(e){return null;}})()";

        private readonly NamedPipeServerStream pipe;
        private readonly string profile;
        private readonly string region;
        private readonly bool clear;
        private readonly WebView2 browser = new WebView2();
        private readonly Label status = new Label();
        private readonly Timer poll = new Timer();
        private readonly List<Form> children = new List<Form>();
        private readonly JavaScriptSerializer json = new JavaScriptSerializer();
        private CoreWebView2Environment environment;
        private StreamWriter writer;
        private bool closing;
        private bool finished;
        private bool polling;
        private bool ready;
        private int navigation;
        public int Outcome { get; private set; }

        public LoginWindow(NamedPipeServerStream pipe, string profile, string region, bool clear)
        {
            this.pipe = pipe;
            this.profile = profile;
            this.region = region;
            this.clear = clear;
            Text = "连接 Apple Music 资料库";
            Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            AutoScaleDimensions = new SizeF(96, 96);
            AutoScaleMode = AutoScaleMode.Dpi;
            Font = new Font("Microsoft YaHei UI", 10);
            ClientSize = new Size(1000, 720);
            MinimumSize = new Size(720, 540);
            StartPosition = FormStartPosition.CenterScreen;
            if (clear)
            {
                Opacity = 0;
                ShowInTaskbar = false;
            }
            var strip = new Panel { Dock = DockStyle.Top, Height = 46, BackColor = Color.FromArgb(239, 245, 242) };
            status.Dock = DockStyle.Fill;
            status.TextAlign = ContentAlignment.MiddleLeft;
            status.Padding = new Padding(14, 0, 8, 0);
            status.Text = "正在打开 Apple Music…";
            var retry = new Button { Text = "连接资料库", Dock = DockStyle.Right, Width = 120 };
            retry.Click += async delegate { await ReadToken(); };
            strip.Controls.Add(status);
            strip.Controls.Add(retry);
            browser.Dock = DockStyle.Fill;
            Controls.Add(browser);
            Controls.Add(strip);
            poll.Interval = 1000;
            poll.Tick += async delegate { await ReadToken(); };
            Shown += async delegate
            {
                if (!clear)
                {
                    Rectangle area = Screen.FromControl(this).WorkingArea;
                    double scale = Math.Max(1, GetDpiForWindow(Handle) / 96.0);
                    int width = Math.Min((int)(1000 * scale), area.Width - 40);
                    int height = Math.Min((int)(720 * scale), area.Height - 40);
                    SetBounds(area.Left + (area.Width - width) / 2, area.Top + (area.Height - height) / 2, width, height);
                }
                await Initialize();
            };
            FormClosing += delegate
            {
                closing = true;
                poll.Stop();
                if (!finished)
                    Send(new { type = "cancelled" });
                foreach (Form child in children.ToArray())
                    child.Close();
            };
            FormClosed += delegate
            {
                poll.Dispose();
                browser.Dispose();
                if (writer != null)
                    writer.Dispose();
            };
        }

        private async Task Initialize()
        {
            try
            {
                await pipe.WaitForConnectionAsync();
                if (closing)
                    return;
                writer = new StreamWriter(pipe, new UTF8Encoding(false), 4096, true) { AutoFlush = true };
                environment = await CoreWebView2Environment.CreateAsync(null, profile);
                await browser.EnsureCoreWebView2Async(environment);
                if (closing)
                    return;
                if (clear)
                {
                    await browser.CoreWebView2.Profile.ClearBrowsingDataAsync();
                    finished = Send(new { type = "cleared" });
                    Close();
                    return;
                }
                ConfigureBrowser(browser, this);
                browser.CoreWebView2.NavigationStarting += delegate { navigation++; };
                browser.CoreWebView2.NavigationCompleted += async delegate(object sender, CoreWebView2NavigationCompletedEventArgs e)
                {
                    if (MusicOrigin(browser.CoreWebView2.Source) && e.IsSuccess)
                    {
                        status.Text = "在 Apple Music 登录后，将自动连接资料库。";
                        if (!ready)
                        {
                            ready = true;
                            Send(new { type = "ready" });
                        }
                        await ReadToken();
                    }
                    else if (MusicOrigin(browser.CoreWebView2.Source))
                    {
                        status.Text = "Apple Music 网页暂时未能加载，请关闭此窗口后重试。";
                    }
                };
                browser.CoreWebView2.Navigate("https://music.apple.com/" + region + "/home");
                poll.Start();
            }
            catch
            {
                if (!closing)
                {
                    Outcome = 1;
                    finished = Send(new { type = "error", message = "无法打开 Apple Music 登录窗口，请检查 WebView2 和网络后重试。" });
                    Close();
                }
            }
        }

        private static bool MusicOrigin(string value)
        {
            Uri uri;
            return Uri.TryCreate(value, UriKind.Absolute, out uri) && uri.Scheme == "https"
                && uri.IsDefaultPort && uri.UserInfo.Length == 0
                && uri.Host.Equals("music.apple.com", StringComparison.OrdinalIgnoreCase);
        }

        private static bool ApplePage(string value)
        {
            Uri uri;
            return value == "about:blank" || (Uri.TryCreate(value, UriKind.Absolute, out uri)
                && uri.Scheme == "https" && uri.IsDefaultPort && uri.UserInfo.Length == 0
                && AllowedHosts.Contains(uri.Host));
        }

        private void ConfigureBrowser(WebView2 view, Form owner)
        {
            view.CoreWebView2.Settings.AreDevToolsEnabled = false;
            view.CoreWebView2.Settings.AreHostObjectsAllowed = false;
            view.CoreWebView2.Settings.IsWebMessageEnabled = false;
            view.CoreWebView2.DownloadStarting += delegate(object sender, CoreWebView2DownloadStartingEventArgs e) { e.Cancel = true; };
            view.CoreWebView2.NavigationStarting += delegate(object sender, CoreWebView2NavigationStartingEventArgs e)
            {
                if (!ApplePage(e.Uri))
                {
                    e.Cancel = true;
                    status.Text = "请在 Apple Music 或 Apple 账户页面继续登录。";
                }
            };
            view.CoreWebView2.WindowCloseRequested += delegate { owner.Close(); };
            view.CoreWebView2.NewWindowRequested += async delegate(object sender, CoreWebView2NewWindowRequestedEventArgs e)
            {
                e.Handled = true;
                if (closing || !ApplePage(e.Uri))
                    return;
                using (var deferral = e.GetDeferral())
                {
                    Form child = null;
                    try
                    {
                        child = new Form { Text = "Apple 账户", Width = 660, Height = 780, StartPosition = FormStartPosition.CenterParent };
                        var childBrowser = new WebView2 { Dock = DockStyle.Fill };
                        child.Controls.Add(childBrowser);
                        children.Add(child);
                        child.FormClosed += delegate { children.Remove(child); childBrowser.Dispose(); };
                        child.Show(owner);
                        await childBrowser.EnsureCoreWebView2Async(environment);
                        if (closing || child.IsDisposed)
                            return;
                        ConfigureBrowser(childBrowser, child);
                        e.NewWindow = childBrowser.CoreWebView2;
                    }
                    catch
                    {
                        if (child != null)
                            child.Close();
                        if (!closing)
                            status.Text = "无法打开 Apple 验证窗口，请重试。";
                    }
                }
            };
        }

        private async Task ReadToken()
        {
            if (closing || finished || polling || browser.CoreWebView2 == null
                || !MusicOrigin(browser.CoreWebView2.Source))
                return;
            polling = true;
            int currentNavigation = navigation;
            try
            {
                string encoded = await browser.CoreWebView2.ExecuteScriptAsync(TokenScript);
                if (closing || currentNavigation != navigation || !MusicOrigin(browser.CoreWebView2.Source)
                    || encoded == null || encoded.Length > 16384)
                    return;
                string value = json.DeserializeObject(encoded) as string;
                if (value == null || !Regex.IsMatch(value, @"\A[\x21-\x7e]{50,8192}\z"))
                    return;
                finished = Send(new { type = "token", value = value });
                if (finished)
                    Close();
            }
            catch
            {
                if (!closing)
                    status.Text = "在 Apple Music 登录后，将自动连接资料库。";
            }
            finally
            {
                polling = false;
            }
        }

        private bool Send(object message)
        {
            if (writer == null || !pipe.IsConnected)
                return false;
            try
            {
                writer.WriteLine(json.Serialize(message));
                return true;
            }
            catch (IOException)
            {
                return false;
            }
        }
    }
}
