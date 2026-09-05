import os
import shutil
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path


class LauncherSafetyTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="smartproxy-launcher-"))
        self.addCleanup(shutil.rmtree, self.project, True)
        (self.project / "scripts").mkdir(parents=True)
        (self.project / ".venv" / "bin").mkdir(parents=True)
        (self.project / "config").mkdir()
        (self.project / "tmp").mkdir()
        shutil.copy2(
            "scripts/start_proxy.sh", self.project / "scripts" / "start_proxy.sh"
        )
        python = self.project / ".venv" / "bin" / "python"
        python.write_text(
            "#!/bin/bash\n"
            "if [ \"$1\" = \"-c\" ]; then\n"
            "  case \"$2\" in *shutdown_deadline*) echo 7 ;; *) echo 7123 ;; esac\n"
            "  exit 0\n"
            "fi\n"
            "sleep 20\n",
            encoding="utf-8",
        )
        python.chmod(0o755)
        self.env = os.environ.copy()
        self.env["TMPDIR"] = str(self.project / "tmp")

    def _run(self, command):
        return subprocess.run(
            [str(self.project / "scripts" / "start_proxy.sh"), command],
            cwd=self.project,
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def test_reused_live_pid_is_refused_for_start_stop_and_status(self):
        unrelated = subprocess.Popen(["sleep", "20"])
        self.addCleanup(self._terminate, unrelated)
        pid_file = self.project / ".smart_proxy.pid"
        pid_file.write_text(str(unrelated.pid), encoding="utf-8")

        for command in ("start", "stop", "status"):
            with self.subTest(command=command):
                completed = self._run(command)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("another process", completed.stdout)
                self.assertTrue(pid_file.exists())
                self.assertIsNone(unrelated.poll())

    def test_stale_pid_is_cleaned_by_status(self):
        pid_file = self.project / ".smart_proxy.pid"
        pid_file.write_text("99999999", encoding="utf-8")

        completed = self._run("status")

        self.assertEqual(completed.returncode, 0)
        self.assertFalse(pid_file.exists())

    def test_status_reports_runtime_config_failure(self):
        (self.project / ".venv" / "bin" / "python").chmod(0o644)

        completed = self._run("status")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Could not read runtime configuration.", completed.stdout)

    def test_backup_uses_configured_port_and_cleans_unique_response_file(self):
        tools = self.project / "tools"
        tools.mkdir()
        capture = self.project / "curl-url.txt"
        curl = tools / "curl"
        curl.write_text(
            "#!/bin/bash\n"
            "out=\n"
            "for ((i=1; i<=$#; i++)); do\n"
            "  arg=${!i}\n"
            "  if [ \"$arg\" = \"-o\" ]; then next=$((i+1)); out=${!next}; fi\n"
            "  case \"$arg\" in http://*) printf '%s' \"$arg\" > \"$CURL_CAPTURE\" ;; esac\n"
            "done\n"
            "printf '%s' '{\"status\":\"success\",\"sources\":1,\"total_proxies\":2}' > \"$out\"\n",
            encoding="utf-8",
        )
        curl.chmod(0o755)
        self.env["PATH"] = f"{tools}:{self.env['PATH']}"
        self.env["CURL_CAPTURE"] = str(capture)

        completed = self._run("backup")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(capture.read_text(encoding="utf-8"), "http://localhost:7123/backup-stats")
        self.assertEqual(list((self.project / "tmp").iterdir()), [])

    @staticmethod
    def _terminate(process):
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
