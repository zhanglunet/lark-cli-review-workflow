import unittest
from unittest.mock import patch

import lark_workflow as workflow


class ReviewDecisionTests(unittest.TestCase):
    def decide(self, text, approval=None, sender="user"):
        config = {"approval": approval or {}, "reviewer_user_id": "synthetic-reviewer"}
        run = {"run_id": "a1b2c3d4e5", "created_at": "2026-01-01T00:00:00Z"}
        with patch.object(workflow, "run_lark", return_value=[
            {"sender": {"sender_type": sender}, "content": {"text": text}}
        ]) as cli:
            result = workflow.find_review_decision(config, run, dry_run=False)
        args = cli.call_args.args[0]
        self.assertEqual(args[args.index("--user-id") + 1], "synthetic-reviewer")
        return result

    def test_explicit_commands(self):
        for text, expected in [
            ("同意 a1b2c3d4e5", "approved"),
            ("APPROVE A1B2C3D4E5", "approved"),
            ("a1b2c3d4e5：同意！", "approved"),
            ("拒绝 a1b2c3d4e5", "rejected"),
            ("REJECT a1b2c3d4e5", "rejected"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(self.decide(text), expected)

    def test_negated_approval_never_approves(self):
        for text in ["不同意", "不批准", "暂不同意", "不要同意", "不能同意",
                     "not approve", "do not approve", "don't approve", "disapprove"]:
            with self.subTest(text=text):
                self.assertNotEqual(self.decide(text + " a1b2c3d4e5"), "approved")

    def test_ambiguous_or_embedded_commands_remain_pending(self):
        for text in ["同意还是拒绝", "同意？", "同意 拒绝", "同意但先别执行",
                     "如果同意", "不同意改成同意", '回复“同意', "approved", "unapproved",
                     "not reject approve"]:
            with self.subTest(text=text):
                self.assertIsNone(self.decide(text + " a1b2c3d4e5"))

    def test_requires_exact_nonempty_task_id(self):
        for text in ["同意", "同意 wrong", "同意 a1b2c3d4e5extra", "同意 xa1b2c3d4e5",
                     "同意 a1b2c3d4e5 another-run"]:
            with self.subTest(text=text):
                self.assertIsNone(self.decide(text))

    def test_custom_commands_and_overlap(self):
        approval = {"approve_words": ["放行", "LGTM"], "reject_words": ["退回", "不同意"]}
        for text, expected in [("放行", "approved"), ("lgtm", "approved"),
                               ("退回", "rejected"), ("不同意", "rejected"),
                               ("不放行", None), ("not lgtm", None), ("同意", None)]:
            with self.subTest(text=text):
                self.assertEqual(self.decide(text + " a1b2c3d4e5", approval), expected)
        self.assertIsNone(self.decide("OK a1b2c3d4e5", {
            "approve_words": ["ok"], "reject_words": ["OK"]}))

    def test_non_user_cannot_decide(self):
        self.assertIsNone(self.decide("同意 a1b2c3d4e5", sender="bot"))

    def test_newer_ambiguous_reply_blocks_older_approval(self):
        messages = [{"sender": {"sender_type": "user"}, "content": {"text": text}}
                    for text in ["暂不同意 a1b2c3d4e5", "同意 a1b2c3d4e5"]]
        with patch.object(workflow, "run_lark", return_value=messages):
            self.assertIsNone(workflow.find_review_decision(
                {"approval": {}, "reviewer_user_id": "synthetic-reviewer"},
                {"run_id": "a1b2c3d4e5", "created_at": "2026-01-01T00:00:00Z"},
                dry_run=False))

    def test_chinese_adjacent_run_mentions_block_older_approval(self):
        for newer in ["a1b2c3d4e5不同意", "任务a1b2c3d4e5 不同意",
                      "任务a1b2c3d4e5待确认", "a1b2c3d4e5？", "（a1b2c3d4e5）暂缓"]:
            with self.subTest(newer=newer):
                self.assertIsNone(self.decide_sequence([newer, "同意 a1b2c3d4e5"]))

    def test_different_run_mentions_do_not_hide_older_valid_decision(self):
        for newer in ["不同意 xa1b2c3d4e5", "不同意 a1b2c3d4e5x",
                      "不同意 0a1b2c3d4e5", "不同意 a1b2c3d4e50",
                      "不同意 _a1b2c3d4e5", "不同意 a1b2c3d4e5_",
                      "不同意 a1b2c3d4e5-other", "不同意 other-a1b2c3d4e5"]:
            with self.subTest(newer=newer):
                self.assertEqual(self.decide_sequence([newer, "同意 a1b2c3d4e5"]), "approved")

    def test_latest_explicit_decision_wins_in_descending_order(self):
        self.assertEqual(self.decide_sequence(["同意 a1b2c3d4e5", "拒绝 a1b2c3d4e5"]), "approved")
        self.assertEqual(self.decide_sequence(["拒绝 a1b2c3d4e5", "同意 a1b2c3d4e5"]), "rejected")

    def test_cli_requests_creation_time_descending_order(self):
        with patch.object(workflow, "run_lark", return_value=[]) as cli:
            workflow.find_review_decision(
                {"approval": {}, "reviewer_user_id": "synthetic-reviewer"},
                {"run_id": "a1b2c3d4e5", "created_at": "2026-01-01T00:00:00Z"}, dry_run=False)
        args = cli.call_args.args[0]
        self.assertIn("--order", args)
        self.assertEqual(args[args.index("--order") + 1], "desc")

    def decide_sequence(self, texts):
        # CLI --order desc supplies newest first; no live conversation is read.
        messages = [{"sender": {"sender_type": "user"}, "content": {"text": text},
                     "create_time": str(2000 - index)} for index, text in enumerate(texts)]
        with patch.object(workflow, "run_lark", return_value=messages):
            result = workflow.find_review_decision(
                {"approval": {}, "reviewer_user_id": "synthetic-reviewer"},
                {"run_id": "a1b2c3d4e5", "created_at": "2026-01-01T00:00:00Z"}, dry_run=False)
        return result

    def test_blank_run_cannot_approve(self):
        with patch.object(workflow, "run_lark", return_value=[
            {"sender": {"sender_type": "user"}, "content": {"text": "同意"}}
        ]):
            self.assertIsNone(workflow.find_review_decision(
                {"approval": {}, "reviewer_user_id": "synthetic-reviewer"},
                {"run_id": "", "created_at": "2026-01-01T00:00:00Z"}, dry_run=False))

    def test_custom_phrases_are_literal_and_whitespace_normalized(self):
        approval = {"approve_words": ["Ship   it", "ok+"], "reject_words": ["send back"]}
        self.assertEqual(self.decide("ship\n it a1b2c3d4e5", approval), "approved")
        self.assertEqual(self.decide("ok+ a1b2c3d4e5", approval), "approved")
        self.assertIsNone(self.decide("okk a1b2c3d4e5", approval))


if __name__ == "__main__":
    unittest.main()
