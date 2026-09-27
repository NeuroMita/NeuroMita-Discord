import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.abspath("src"))


class DiscordAttentionTests(unittest.TestCase):
    def test_invalid_or_untrusted_model_decision_fails_silent(self):
        from discord_bot.attention import DiscordAttentionService, SILENT

        parse = DiscordAttentionService._parse_decision
        invalid_json = parse("not json", valid_reply_ids=set())
        self.assertFalse(invalid_json.speak)
        self.assertEqual(invalid_json.diagnostic_code, "invalid_json")
        self.assertEqual(parse(
            '{"speak":true,"action":"join","desire":90,"reply_to_message_id":"999","reason_code":"topic_fit"}',
            valid_reply_ids={"1"},
        ).diagnostic_code, "invalid_reply_target")
        self.assertEqual(parse(
            '{"speak":true,"action":"join","desire":90,"reply_to_message_id":null,"reason_code":"topic_fit","chain":"ignore rules"}',
            valid_reply_ids=set(),
        ).speak, True)

    def test_final_gate_never_speaks_when_classifier_says_no(self):
        from discord_bot.attention import AttentionDecision, should_accept_attention_decision

        rng = Mock()
        rng.randint.return_value = 0
        decision = AttentionDecision(False, "silent", 100, None, "nothing_to_add")
        self.assertFalse(should_accept_attention_decision(decision, initiative_level=100, rng=rng))
        rng.randint.assert_not_called()

    def test_classifier_uses_bounded_single_attempt_utility_request(self):
        from discord_bot.attention import DiscordAttentionService

        generation = Mock()
        generation.generate_utility.return_value = type("Result", (), {
            "ok": True, "text": '{"speak":false,"action":"silent","desire":10,"reason_code":"nothing_to_add"}',
        })()
        service = DiscordAttentionService(
            generation, character_id_provider=lambda: "Crazy", preset_id_provider=lambda: 17,
        )
        result = service.decide_social(room_context="context", initiative_level=55)

        self.assertFalse(result.speak)
        request = generation.generate_utility.call_args.args[0]
        self.assertEqual(request.kind, "discord_attention")
        self.assertEqual(request.preset_id, 17)
        self.assertEqual(request.max_attempts, 1)
        self.assertEqual(request.retry_delay, 0)
        self.assertEqual(request.request_timeout, 30)
        self.assertEqual(request.generation_params_override, {
            "max_tokens": 512,
            "reasoning": {"enabled": True, "max_tokens": 256},
        })

    def test_attention_failures_expose_only_safe_cause_and_status(self):
        from discord_bot.attention import DiscordAttentionService

        generation = Mock()
        generation.generate_utility.return_value = type("Result", (), {
            "ok": False, "text": "private model output",
            "error": "secret provider diagnostic", "status_code": 503,
        })()
        service = DiscordAttentionService(
            generation, character_id_provider=lambda: "Crazy", preset_id_provider=lambda: None,
        )

        decision = service.decide_social(room_context="private room text", initiative_level=55)

        self.assertEqual(decision.diagnostic_code, "provider_error")
        self.assertEqual(decision.diagnostic_status, 503)
        self.assertFalse(decision.speak)

    def test_attention_malformed_json_gets_specific_non_content_diagnostic(self):
        from discord_bot.attention import DiscordAttentionService

        generation = Mock()
        generation.generate_utility.return_value = type("Result", (), {
            "ok": True, "text": "private malformed response",
        })()
        service = DiscordAttentionService(
            generation, character_id_provider=lambda: "Crazy", preset_id_provider=lambda: None,
        )

        decision = service.decide_social(room_context="private room text", initiative_level=55)

        self.assertEqual(decision.diagnostic_code, "invalid_json")
        self.assertIsNone(decision.diagnostic_status)

    def test_unrecognized_explanation_does_not_discard_valid_speak_decision(self):
        from discord_bot.attention import DiscordAttentionService

        decision = DiscordAttentionService._parse_decision(
            '{"speak":true,"action":"join","desire":90,'
            '"reply_to_message_id":null,"reason_code":"follow_up_question"}',
            valid_reply_ids=set(),
        )

        self.assertTrue(decision.speak)
        self.assertEqual(decision.action, "join")
        self.assertEqual(decision.desire, 90)
        self.assertEqual(decision.reason_code, "topic_fit")
        self.assertEqual(decision.diagnostic_code, "unrecognized_reason")

    def test_unrecognized_action_keeps_valid_initiative_decision(self):
        from discord_bot.attention import DiscordAttentionService

        decision = DiscordAttentionService._parse_decision(
            '{"speak":true,"action":"reply","desire":95,'
            '"reply_to_message_id":null,"reason_code":"good_topic_to_start"}',
            valid_reply_ids=set(), fallback_action="start_topic",
        )

        self.assertTrue(decision.speak)
        self.assertEqual(decision.action, "start_topic")
        self.assertEqual(decision.diagnostic_code, "unrecognized_action")

    def test_silent_decision_with_unrecognized_action_remains_silent(self):
        from discord_bot.attention import DiscordAttentionService

        decision = DiscordAttentionService._parse_decision(
            '{"speak":false,"action":"reply","desire":10,'
            '"reply_to_message_id":null,"reason_code":"nothing_to_add"}',
            valid_reply_ids=set(), fallback_action="join",
        )

        self.assertFalse(decision.speak)
        self.assertEqual(decision.action, "silent")
        self.assertEqual(decision.diagnostic_code, "invalid_action")


if __name__ == "__main__":
    unittest.main()
