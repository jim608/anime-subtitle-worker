from pathlib import Path
import unittest


class SafeUpdateWorkerScriptTest(unittest.TestCase):
    def test_script_waits_for_mikan_and_ai_before_recreate(self) -> None:
        script = Path("safe-update-worker.sh").read_text(encoding="utf-8")

        self.assertIn("POLL_SECONDS must be a positive integer", script)
        self.assertIn('WAIT_FOR_IDLE="${WAIT_FOR_IDLE:-1}"', script)
        self.assertIn("MIKAN_WAIT_SECONDS must be a non-negative integer", script)
        self.assertIn("IDLE_WAIT_SECONDS must be a non-negative integer", script)
        self.assertIn("MIKAN_LOCK_MAX_AGE_SECONDS must be a non-negative integer", script)
        self.assertIn('MIKAN_LOCK_MAX_AGE_SECONDS="${MIKAN_LOCK_MAX_AGE_SECONDS:-43200}"', script)
        self.assertIn('MIKAN_ACTIVE_STALE_SECONDS="${MIKAN_ACTIVE_STALE_SECONDS:-900}"', script)
        self.assertIn("MIKAN_ACTIVE_STALE_SECONDS must be a non-negative integer", script)
        self.assertIn("/work/mikan_redownload_all.active.json", script)
        self.assertIn('bucket.append(("redownload-active"', script)
        self.assertIn("if lock._is_stale_lock()", script)
        self.assertIn("AI_RUNNING_STALE_SECONDS must be a non-negative integer", script)
        self.assertIn("Waiting up to ${IDLE_WAIT_SECONDS}s for Mikan operations before gracefully draining active AI work", script)
        self.assertIn("/work/mikan_worker", script)
        self.assertIn("/work/mikan_enqueue", script)
        self.assertIn("/work/mikan_extract", script)
        self.assertIn("/work/mikan_redownload", script)
        self.assertIn("mikan_busy=", script)
        self.assertIn("ai_busy=", script)
        self.assertIn("not fresh_locks and not active_ai", script)
        self.assertIn("No fresh Mikan operations detected; requesting a graceful Worker stop", script)
        self.assertIn('docker stop --time "$IDLE_WAIT_SECONDS"', script)
        self.assertIn("Idle wait limit reached", script)
        self.assertIn("leaving the current worker running", script)
        self.assertIn("Run with WAIT_FOR_IDLE=0", script)
        self.assertIn("old_locks", script)
        self.assertIn("FROM ai_candidate_queue", script)
        self.assertIn("ai stale-running", script)
        self.assertIn("WAIT_FOR_IDLE=0, force-stopping", script)
        self.assertIn('docker kill "$CONTAINER"', script)
        self.assertLess(
            script.index("docker compose build"),
            script.index("docker compose up -d --no-build --force-recreate"),
        )
        self.assertLess(
            script.index('docker stop --time "$IDLE_WAIT_SECONDS"'),
            script.index("docker compose up -d --no-build --force-recreate"),
        )

    def test_compose_stop_grace_period_is_short(self) -> None:
        compose = Path("docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("stop_grace_period: 60s", compose)
        self.assertNotIn("stop_grace_period: 24h", compose)


if __name__ == "__main__":
    unittest.main()
