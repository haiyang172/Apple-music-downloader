using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows.Forms;

[assembly: AssemblyTitle("amdl")]
[assembly: AssemblyDescription("amdl")]
[assembly: AssemblyProduct("amdl")]

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
            MessageBox.Show("安装文件不完整，请重新运行 amdl 安装程序。",
                "amdl", MessageBoxButtons.OK, MessageBoxIcon.Error);
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
            MessageBox.Show("无法打开 amdl，请检查安装文件。",
                "amdl", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
