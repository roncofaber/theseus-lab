"""The antigravity preset (agy), skill installation, non-preassigned session handling and the antigravity-jsonl
transcript adapter."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import Sandbox

from theseus import adapters, coordinator, profiles, setup, supervisor
from theseus import config as cfg
from theseus.adapters import antigravity


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


class AntigravityProfileTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.scaffold()
        self.write("theseus.local.toml", '[workers.agy-custom]\npreset = "antigravity"\nmodel = "gemini-4-neon-medium"\n'
                   'permission_mode = "accept-edits"\nextra_args = ["--sandbox"]\n')
        self.conf = cfg.load(root=self.project)

    def test_preset_alias_and_headless_resume_argv(self):
        plain = profiles.resolve(self.conf, "antigravity")
        alias = profiles.resolve(self.conf, "agy")
        self.assertEqual("agy", plain["command"])
        self.assertEqual("antigravity", alias["preset"])
        self.assertFalse(plain["verified"])
        self.assertEqual("none", plain["session"])
        self.assertEqual(1048576, profiles.model_context_limit(self.conf, "gemini-4-neon-medium"))
        self.assertEqual(["agy", "-p", "do it", "--output-format", "json", "--mode", "auto"],
                         profiles.launch_argv(plain, "do it", "", "", self.conf))
        self.assertEqual(["agy", "-p", "more", "--continue", "--output-format", "json", "--mode", "auto"],
                         [x.replace("{message}", "more") for x in profiles.resume_template(plain, "", "", self.conf)])

        custom = profiles.resolve(self.conf, "agy-custom")
        self.assertEqual(["agy", "-p", "do it", "--output-format", "json", "--mode", "accept-edits",
                          "--model", "gemini-4-neon-medium", "--sandbox"],
                         profiles.launch_argv(custom, "do it", "", custom["model"], self.conf))
        self.assertEqual([], profiles.problems("agy-custom", {
            "preset": "antigravity", "model": "gemini-4-neon-medium",
            "interactive": {"transcript": "antigravity-jsonl"},
        }))

    def test_interactive_launch_resume_and_coordinator_argv(self):
        plain = profiles.resolve(self.conf, "antigravity")
        argv, env = profiles.interactive_argv(self.conf, plain, "agy-1")
        self.assertEqual(["agy", "--mode", "auto"], argv)
        self.assertEqual({}, env)
        argv, _ = profiles.interactive_argv(self.conf, plain, "agy-1", session="conv-123", resume=True)
        self.assertEqual(["agy", "--mode", "auto", "--conversation", "conv-123"], argv)

        custom = profiles.resolve(self.conf, "agy-custom")
        argv, _ = profiles.interactive_argv(self.conf, custom, "agy-2")
        self.assertEqual(["agy", "--mode", "accept-edits", "--model", "gemini-4-neon-medium", "--sandbox"], argv)
        coord_argv, _ = coordinator.launch(self.conf, "agy-custom")
        self.assertEqual(["agy", "--mode", "accept-edits", "--model", "gemini-4-neon-medium", "--sandbox"], coord_argv)

    def test_setup_skill_and_rea_skill_location_for_antigravity(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp)}):
            code, lines = setup.install_skill(client="antigravity")
            self.assertEqual(0, code, lines)
            installed = self.tmp / ".gemini" / "config" / "skills" / "theseus-setup" / "SKILL.md"
            self.assertTrue(installed.is_file())
            rea_skill = self.tmp / ".gemini" / "config" / "skills" / setup.SKILL / "SKILL.md"
            rea_skill.parent.mkdir(parents=True, exist_ok=True)
            rea_skill.write_text("# rea\n")
            self.assertEqual(rea_skill.parent, setup.skill_location("antigravity"))

    def test_resume_session_returns_none_when_no_session_or_no_resume_template(self):
        self.write("theseus.local.toml", '[workers.noresume]\npreset = "antigravity"\n'
                   '[workers.noresume.interactive]\nresume = ""\n')
        conf = cfg.load(root=self.project)
        from theseus import store
        with store.for_project(conf) as conn:
            store.upsert_agent(conn, conf.name, "a1", pane="%1", profile="antigravity", source="supervisor")
            store.update_agent(conn, conf.name, "a1", managed=True, session_id="")
            store.upsert_agent(conn, conf.name, "a2", pane="%2", profile="noresume", source="supervisor")
            store.update_agent(conn, conf.name, "a2", managed=True, session_id="conv-1")
        self.assertIsNone(supervisor.resume_session(conf, "a1", "restart"))
        self.assertIsNone(supervisor.resume_session(conf, "a2", "restart"))


class AntigravityTranscriptTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.brain = self.tmp / "brain"
        self.conv = "5bc47dc4-2e42-4262-9e5f-a1a4f6fa8553"
        self.path = antigravity.transcript_path(self.brain, self.conv)
        self.steps = [
            {
                "step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE",
                "created_at": "2026-11-19T18:20:48Z",
                "content": "<USER_REQUEST>\nReview this repo.\n</USER_REQUEST>\n"
                           "<ADDITIONAL_METADATA>\nTime\n</ADDITIONAL_METADATA>\n"
                           "<USER_SETTINGS_CHANGE>\nChanged\n</USER_SETTINGS_CHANGE>",
            },
            {
                "step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                "created_at": "2026-11-19T18:20:49Z", "model": "gemini-4-neon-medium",
                "input_tokens": 4158, "cache_read_tokens": 8192, "output_tokens": 353,
                "thinking": "Internal reasoning that must stay out of entries.",
                "tool_calls": [{"name": "read_file", "args": {"Path": '"/work/README.md"'}}],
            },
            {
                "step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "RUNNING",
                "created_at": "2026-11-19T18:20:50Z", "content": "still running",
            },
            {
                "step_index": 3, "source": "MODEL", "type": "GENERIC", "status": "DONE",
                "created_at": "2026-11-19T18:20:51Z",
                "content": "Created At: 2026-11-19T10:20:50-08:00\nCompleted At: 2026-11-19T10:20:51-08:00\n# theseus-lab\n",
            },
            {
                "step_index": 4, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "status": "DONE",
                "created_at": "2026-11-19T18:20:52Z", "content": "Background task finished",
            },
            {
                "step_index": 5, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                "created_at": "2026-11-19T18:20:53Z", "model": "gemini-4-neon-medium",
                "input_tokens": 5000, "cache_read_tokens": 8000, "output_tokens": 100,
                "content": "Looks good.",
            },
        ]
        write_jsonl(self.path, self.steps)

    def test_adapter_parses_conversation_tokens_model_and_skips_thinking_and_system(self):
        self.assertIs(antigravity, adapters.load("antigravity-jsonl"))
        state = antigravity.read(self.path)
        self.assertEqual((13100, "gemini-4-neon-medium", "answer"), (state["tokens"], state["model"], state["last"]))
        self.assertEqual(["user", "tool", "result", "assistant"], [e["kind"] for e in state["entries"]])
        self.assertEqual("Review this repo.", state["entries"][0]["text"])
        self.assertEqual(("read_file", "/work/README.md"), (state["entries"][1]["name"], state["entries"][1]["summary"]))
        self.assertEqual("# theseus-lab", state["entries"][2]["summary"])
        self.assertFalse(state["entries"][2]["error"])
        self.assertEqual(("Looks good.", "markdown"), (state["entries"][3]["text"], state["entries"][3]["format"]))
        self.assertEqual([1, 2, 3, 4], [e["n"] for e in state["entries"]])

    def test_incremental_append_and_error_steps(self):
        state = antigravity.read(self.path)
        self.assertEqual(4, state["total"])
        with open(self.path, "a") as handle:
            handle.write(json.dumps({
                "step_index": 6, "source": "MODEL", "type": "GENERIC", "status": "ERROR",
                "created_at": "2026-11-19T18:21:00Z", "content": "Permission denied",
            }) + "\n")
            handle.write(json.dumps({
                "step_index": 7, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "ERROR",
                "created_at": "2026-11-19T18:21:01Z", "content": "API Error: 429 rate limit exceeded",
            }) + "\n")
        state = antigravity.read(self.path)
        self.assertEqual("error", state["last"])
        self.assertEqual(1, len(state["errors"]))
        self.assertIn("429", state["errors"][0]["text"])
        self.assertTrue(state["entries"][4]["error"])
        self.assertEqual([1, 2, 3, 4, 5, 6], [e["n"] for e in state["entries"]])

    def test_session_file_and_session_state(self):
        interactive = {"sessions_dir": str(self.brain)}
        found = antigravity.session_file(None, interactive, self.conv, "/anywhere")
        self.assertEqual(self.path, found)
        self.assertIsNone(antigravity.session_file(None, interactive, "missing-id", "/anywhere"))
        agent = {"name": "agy-1", "managed": True, "session_id": self.conv}
        self.assertEqual(13100, antigravity.session_state(None, agent, interactive)["tokens"])
        self.assertIsNone(antigravity.session_state(None, {**agent, "managed": False}, interactive))

    def test_live_session_is_the_presence_lock_the_process_holds(self):
        presence = self.brain.parent / "presence"
        presence.mkdir(parents=True)
        conv = "0123abcd-4567-89ab-cdef-0123456789ab"
        (presence / "ffffffff-0000-0000-0000-000000000000.lock").touch()
        child = subprocess.Popen([sys.executable, "-c", "import sys, time; h = open(sys.argv[1], 'a'); print('ok', "
                                  "flush=True); time.sleep(30)", str(presence / f"{conv}.lock")],
                                 stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual("ok", child.stdout.readline().strip())
            interactive = {"sessions_dir": str(self.brain)}
            self.assertEqual(conv, antigravity.live_session(interactive, {os.getpid(), child.pid}))
            self.assertIsNone(antigravity.live_session(interactive, {os.getpid()}))
            self.assertIsNone(antigravity.live_session({"sessions_dir": str(self.tmp / "elsewhere/brain")}, {child.pid}),
                              "a lock outside the profile's app data directory is not the agent's")
        finally:
            child.kill()
            child.wait()
            child.stdout.close()

    def test_state_records_when_the_transcript_was_written(self):
        state = antigravity.read(self.path)
        self.assertAlmostEqual(self.path.stat().st_mtime, state["updated"])


class SessionDiscoveryTests(Sandbox):
    def test_the_supervisor_learns_the_conversation_from_the_adapter_without_a_status_command(self):
        from unittest import mock
        from theseus import workqueue
        self.scaffold()
        conf = cfg.load(root=self.project)
        rec = {"name": "agy-1", "target": "demo--agy-1", "profile": "antigravity", "server": None}
        found = []
        with mock.patch.object(supervisor, "pane_info", lambda srv, target: {"pane_pid": "4242"}), \
                mock.patch.object(supervisor, "server_of", lambda rec: None), \
                mock.patch.object(supervisor, "status_of", lambda conf, profile, pid: None), \
                mock.patch.object(workqueue, "launched", lambda conf, rec: "antigravity"), \
                mock.patch.object(antigravity, "live_session",
                                  lambda interactive, pids: found.append(pids) or "conv-from-lock"):
            self.assertEqual("conv-from-lock", supervisor.live_session(conf, rec))
        self.assertIn(4242, found[0])


class BusyPatternTests(Sandbox):
    def test_a_profile_busy_pattern_wins_over_a_project_wide_one(self):
        self.scaffold()
        self.replace_config("[instances]", '[instances]\nbusy_pattern = "esc to interrupt"')
        conf = cfg.load(root=self.project)
        self.assertEqual("esc to cancel", profiles.instance_env(conf, "antigravity")["THESEUS_BUSY_PATTERN"])
        self.assertEqual("esc to interrupt", profiles.instance_env(conf, "codex")["THESEUS_BUSY_PATTERN"],
                         "a profile without its own marker falls back to [instances]")


if __name__ == "__main__":
    unittest.main()
