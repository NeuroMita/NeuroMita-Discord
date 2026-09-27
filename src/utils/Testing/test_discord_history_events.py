import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, os.path.abspath("src"))


class DiscordAmbientHistoryTests(unittest.TestCase):
    def test_autonomous_character_turn_does_not_persist_a_fake_empty_user_event(self):
        from managers.conversation_event_writer import ConversationEventWriter
        from domain.dialogue_identity import DialogueActorKind

        writer = ConversationEventWriter.__new__(ConversationEventWriter)
        writer._identity_resolver = Mock()
        writer._identity_resolver.resolve.return_value = SimpleNamespace(
            kind=DialogueActorKind.UNKNOWN, sender_id="DiscordRoom",
        )
        writer._dialogue_metadata = Mock(return_value={})
        writer.normalize_participants = Mock(return_value=[])
        writer._history_recipient_ids = Mock(return_value=["Crazy"])
        writer._build_user_event_message = Mock(return_value={"role": "user", "content": ""})
        writer._build_assistant_event_message = Mock(return_value={"role": "assistant", "content": "hello"})
        writer._fanout_turn = Mock(return_value=([], []))

        writer.write_turn(
            responder_character_id="Crazy", sender="DiscordRoom", participants=[],
            user_input="", image_data=[], req_id="discord-social-1", origin_message_id=None,
            assistant_text="hello", assistant_target="DiscordRoom", event_type="discord_social",
            task_uid=None,
        )

        writer._build_user_event_message.assert_not_called()
        writer._fanout_turn.assert_called_once_with(None, {"role": "assistant", "content": "hello"}, ["Crazy"])


if __name__ == "__main__":
    unittest.main()
