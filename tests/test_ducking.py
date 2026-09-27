import io
import subprocess
import unittest
from unittest.mock import Mock, patch

from jarvis.speech.ducking import SystemAudioMute


def stream(index, pid, muted=False, serial=None):
    return {"index": index, "mute": muted, "properties": {
        "application.process.id": str(pid), "object.serial": str(serial or index),
    }}


class DuckingTests(unittest.TestCase):
    def test_other_streams_muted_and_original_states_restored(self):
        mute = SystemAudioMute(100)
        initial = [stream(1, 100), stream(2, 200), stream(3, 300, True)]
        with patch.object(mute, "_streams", return_value=initial), patch.object(mute, "_set_mute") as change:
            mute._refresh()
            self.assertEqual(change.call_args_list[0].args, (2, True))
            self.assertEqual(change.call_count, 1)
            mute.close()
            self.assertEqual([call.args for call in change.call_args_list], [(2, True), (2, False), (3, True)])
            mute.close()
            self.assertEqual(change.call_count, 3)

    def test_new_streams_muted_and_removed_streams_not_restored(self):
        mute = SystemAudioMute(100)
        with patch.object(mute, "_streams", side_effect=[[stream(2, 200)], [stream(4, 400)], [stream(4, 400, True)]]), patch.object(mute, "_set_mute") as change:
            mute._refresh()
            mute._refresh()
            mute.close()
        self.assertEqual([call.args for call in change.call_args_list], [(2, True), (4, True), (4, False)])

    def test_reused_index_does_not_restore_an_unrelated_stream(self):
        mute = SystemAudioMute(100)
        with patch.object(mute, "_streams", side_effect=[[stream(2, 200)], [stream(2, 999, True, 99)]]), patch.object(mute, "_set_mute") as change:
            mute._refresh()
            mute.close()
        change.assert_called_once_with(2, True)

    def test_partial_initialization_failure_restores_audio_and_stops_subscription(self):
        mute = SystemAudioMute(100)
        subscription = Mock(stdout=io.StringIO())
        with patch("jarvis.speech.ducking.subprocess.Popen", return_value=subscription), patch("jarvis.speech.ducking.terminate_process") as terminate, patch.object(mute, "_streams", return_value=[stream(2, 200)]), patch.object(mute, "_set_mute", side_effect=[subprocess.CalledProcessError(1, "pactl"), None]) as change:
            with self.assertRaises(subprocess.CalledProcessError):
                mute.start()
        self.assertEqual([call.args for call in change.call_args_list], [(2, True), (2, False)])
        terminate.assert_called_once_with(subscription, group=True)

    def test_subscription_refreshes_on_sink_input_events(self):
        mute = SystemAudioMute(100)
        mute._subscription = Mock(stdout=io.StringIO("Event 'new' on sink-input #4\nEvent 'change' on client #5\n"))
        with patch.object(mute, "_refresh") as refresh:
            mute._watch()
        refresh.assert_called_once()

    def test_restore_failure_does_not_prevent_restoring_other_streams(self):
        mute = SystemAudioMute(100)
        mute._saved = {2: (("200", "2"), False), 3: (("300", "3"), False)}
        with patch.object(mute, "_streams", return_value=[stream(2, 200, True), stream(3, 300, True)]), patch.object(mute, "_set_mute", side_effect=[RuntimeError("removed"), None]) as change:
            mute.close()
        self.assertEqual(change.call_count, 2)
