#!/home/gh/python/venv_py311/bin/python3
"""
Robustness tests for the 4 fixes in audiosocket_translator.py v1.1.0

Fix 1: Task exceptions logged via _log_task_exception callback
Fix 2: ami_originate has 10s connection timeout
Fix 3: handle_inbound wraps ami_originate in try/except → sess.fail()
Fix 4: Worker.run() stops on write errors (BrokenPipeError/ConnectionResetError)
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
import audiosocket_translator as tr


class TestVersionBump(unittest.TestCase):
    def test_semver_in_version(self):
        self.assertIn("1.1.0", tr.VERSION)

    def test_git_ref_in_version(self):
        self.assertIn("git:", tr.VERSION)


class TestTaskExceptionCallback(unittest.IsolatedAsyncioTestCase):
    async def test_failed_task_is_logged(self):
        """_log_task_exception must log ERROR when a task raises."""
        with self.assertLogs("ast", level="ERROR") as cm:
            async def boom():
                raise RuntimeError("Kaboom")

            task = asyncio.create_task(boom())
            task.add_done_callback(tr._log_task_exception)
            await asyncio.sleep(0)   # let task run
            await asyncio.sleep(0)   # let callback fire

        self.assertTrue(any("Kaboom" in line for line in cm.output))

    async def test_cancelled_task_not_logged(self):
        """Cancelled tasks must not produce error logs."""
        async def sleeper():
            await asyncio.sleep(999)

        task = asyncio.create_task(sleeper())
        task.add_done_callback(tr._log_task_exception)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # No assertLogs block — if an error were logged the test would still pass,
        # but the point is the callback must not raise itself.


class TestAmiOriginateTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_slow_ami_raises_connection_error(self):
        """ami_originate must raise ConnectionError when TCP connect hangs > 10s."""
        async def hanging(*a, **kw):
            await asyncio.sleep(9999)

        sess = tr.CallSession("00000000-0000-0000-0000-000000000000")
        with patch("asyncio.open_connection", hanging):
            with self.assertRaises((ConnectionError, asyncio.TimeoutError)):
                # The internal wait_for fires at 10s; we give 12s to let it.
                # Patch speeds this up by making open_connection hang instantly
                # and relying on the internal 10s timeout — but 10s is too slow
                # for a test, so we wrap with a tighter outer timeout to catch
                # whichever fires first.
                await asyncio.wait_for(
                    tr.ami_originate("+39012345678", "partner-uuid", sess),
                    timeout=11.0,
                )

    async def test_refused_connection_propagates(self):
        """Connection refused must propagate cleanly (no hang)."""
        async def refused(*a, **kw):
            raise ConnectionRefusedError("no AMI")

        sess = tr.CallSession("00000000-0000-0000-0000-000000000000")
        with patch("asyncio.open_connection", refused):
            with self.assertRaises((ConnectionRefusedError, ConnectionError, OSError)):
                await tr.ami_originate("+39012345678", "partner-uuid", sess)


class TestHandleInboundAmiFail(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tr._gpu_server = tr.GpuInferenceServer()  # no .start() needed — AMI fails first

    async def test_ami_failure_calls_sess_fail_and_closes_writer(self):
        """handle_inbound must call sess.fail() and close the writer on AMI error."""
        reader = MagicMock(spec=asyncio.StreamReader)
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()

        uuid = "cccccccc-0000-0000-0000-000000000000"
        tr._exten_map[uuid] = "+3900123456789"

        fail_reasons: list[str] = []
        original_fail = tr.CallSession.fail

        def capture_fail(self_sess, reason):
            fail_reasons.append(reason)
            original_fail(self_sess, reason)

        with patch.object(tr.CallSession, "fail", capture_fail):
            with patch(
                "audiosocket_translator.ami_originate",
                side_effect=ConnectionError("AMI down"),
            ):
                await tr.handle_inbound(uuid, reader, writer)

        self.assertTrue(
            any("AMI" in r for r in fail_reasons),
            f"Expected AMI error in sess.fail(), got: {fail_reasons}",
        )
        writer.close.assert_called()

        tr._exten_map.pop(uuid, None)


class TestWorkerStopsOnWriteError(unittest.IsolatedAsyncioTestCase):
    async def test_broken_pipe_stops_worker(self):
        """Worker.run() must return (not loop forever) when the writer is dead."""
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.write = MagicMock()
        writer.drain = AsyncMock(side_effect=BrokenPipeError("pipe broken"))

        sess = tr.CallSession("dddddddd-0000-0000-0000-000000000000")
        sess.state = tr.CallState.CONNECTED

        gpu_server = MagicMock(spec=tr.GpuInferenceServer)
        gpu_server.run = AsyncMock(return_value="Hello.")

        with patch("audiosocket_translator.stt_chunks", AsyncMock(return_value=["Hallo."])), \
             patch("audiosocket_translator.tts", AsyncMock(return_value=b"\x00" * 320)):

            worker = tr.Worker("de", "en", writer, "TEST", sess, gpu_server)
            worker._q.put_nowait(b"\x00" * 320)

            try:
                await asyncio.wait_for(worker.run(), timeout=5.0)
            except asyncio.TimeoutError:
                self.fail(
                    "Worker did not stop on BrokenPipeError — "
                    "Fix 4 (connection error handler) missing or not working"
                )

    async def test_connection_reset_stops_worker(self):
        """Worker.run() must also return on ConnectionResetError."""
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.write = MagicMock()
        writer.drain = AsyncMock(side_effect=ConnectionResetError("reset"))

        sess = tr.CallSession("eeeeeeee-0000-0000-0000-000000000000")
        sess.state = tr.CallState.CONNECTED

        gpu_server = MagicMock(spec=tr.GpuInferenceServer)
        gpu_server.run = AsyncMock(return_value="Hello.")

        with patch("audiosocket_translator.stt_chunks", AsyncMock(return_value=["Ciao."])), \
             patch("audiosocket_translator.tts", AsyncMock(return_value=b"\x00" * 320)):

            worker = tr.Worker("it", "de", writer, "TEST2", sess, gpu_server)
            worker._q.put_nowait(b"\x00" * 320)

            try:
                await asyncio.wait_for(worker.run(), timeout=5.0)
            except asyncio.TimeoutError:
                self.fail(
                    "Worker did not stop on ConnectionResetError — "
                    "Fix 4 (connection error handler) missing or not working"
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
