using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows.Forms;

[assembly: AssemblyTitle("影音下载台")]
[assembly: AssemblyDescription("Media Download Desk")]
[assembly: AssemblyProduct("影音下载台")]

internal static class Launcher
{
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    private static extern int SetCurrentProcessExplicitAppUserModelID(string appId);

    [STAThread]
    private static void Main()
    {
        SetCurrentProcessExplicitAppUserModelID("amdl.desktop");
        string home = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(home, "runtime", "pythonw.exe");
        string studio = Path.Combine(home, "Studio.py");
        if (!File.Exists(python) || !File.Exists(studio))
        {
            MessageBox.Show("安装文件不完整，请重新运行影音下载台安装程序。",
                "影音下载台", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }

        try
        {
            var info = new ProcessStartInfo
            {
                FileName = python,
                Arguments = "-E -s -B \"" + studio + "\"",
                WorkingDirectory = home,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            using (Process process = Process.Start(info))
            {
                if (process == null)
                    throw new InvalidOperationException();
            }
        }
        catch (Exception)
        {
            MessageBox.Show("无法打开影音下载台，请检查安装文件。",
                "影音下载台", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
