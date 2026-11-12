using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("AMDL Studio")]
[assembly: AssemblyDescription("AMDL Studio for Windows")]
[assembly: AssemblyProduct("AMDL Studio")]

internal static class Launcher
{
    [STAThread]
    private static void Main()
    {
        string home = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(home, "runtime", "pythonw.exe");
        string studio = Path.Combine(home, "Studio.py");
        if (!File.Exists(python) || !File.Exists(studio))
        {
            MessageBox.Show("安装文件不完整，请重新运行 AMDL Studio 安装程序。",
                "AMDL Studio", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }

        try
        {
            var info = new ProcessStartInfo
            {
                FileName = python,
                Arguments = "-B \"" + studio + "\"",
                WorkingDirectory = home,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            info.EnvironmentVariables.Remove("PYTHONHOME");
            info.EnvironmentVariables["PYTHONPATH"] = Path.Combine(home, "lib");
            info.EnvironmentVariables["PYTHONNOUSERSITE"] = "1";
            using (Process process = Process.Start(info))
            {
                if (process == null)
                    throw new InvalidOperationException();
            }
        }
        catch (Exception)
        {
            MessageBox.Show("无法打开 AMDL Studio，请检查安装文件。",
                "AMDL Studio", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
