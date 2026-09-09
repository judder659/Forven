using System;
using System.IO;
using System.Management.Automation;
using System.Windows.Forms;

internal static class ForvenLauncher
{
    [STAThread]
    private static void Main()
    {
        try
        {
            using (PowerShell shell = PowerShell.Create())
            {
                shell.AddCommand(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "launcher.ps1"));
                shell.Invoke();
                if (shell.HadErrors)
                    MessageBox.Show(shell.Streams.Error[0].ToString(), "Forven Launcher", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "Forven Launcher", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
