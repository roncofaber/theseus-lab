"""The attention list and its push: items follow the state they are derived from, the queue driver types one line per
change into the registered coordinator pane (never into a prompt, one reminder) and escalation runs its command once.
Panes are a fake tmux whose typed input is logged; times come from the caller, never from waiting."""
import json
import os
import time

from helpers import Sandbox, fake_clock
from test_queue import FAKE_TMUX

TASK = "parse-capture-header"


def item(key, kind, label, since):
    return {"key": key, "kind": kind, "project": "demo-port", "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since)),
            "label": label, "summary": f"{key} needs you", "command": "theseus attention"}


class AttentionItemTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.scaffold()
        self.write("theseus.local.toml", "[paths]\noriginal = []\n")
        self.theseus("tasks", "milestone", "M0")

    def attention(self):
        return json.loads(self.theseus("attention", "--json").stdout)

    def test_a_delivery_shows_until_it_is_merged_and_the_merge_then_shows_until_verified(self):
        self.theseus("tasks", "add", TASK, "--role", "implementation", "--owned", "src/capture/header.py",
                     f"docs/investigations/{TASK}.md", "--objective", "Parse the header.", "--contract", "Every variant.", "--accept", "Two variants parse.")
        self.assertEqual("nothing needs you", self.attention()["summary"])
        self.theseus("instance", "assign", TASK, "worker-1", "a")
        worktree = self.tmp / "worktrees/worker-1-a"
        self.write("src/capture/header.py", "def parse(data):\n    return data[:4]\n", root=worktree)
        self.write(f"docs/investigations/{TASK}.md", f"# Header\n\nTask `{TASK}` on base x. Topic: `new`.\n\n## Result\n\n- "
                   f"**Recovered**: the magic.\n", root=worktree)
        handoff = self.write(".theseus/handoff.md", f"Task: {TASK}\nResult: delivered\nChecks: none\n", root=worktree)
        self.assertEqual([], self.attention()["items"], "a task in progress needs nobody")
        self.theseus("tasks", "deliver", TASK, "--handoff", handoff)
        listed = self.attention()
        self.assertEqual("1 item needs you (1 delivery)", listed["summary"])
        [delivery] = listed["items"]
        self.assertEqual((f"delivery:{TASK}", "delivery", "demo-port"), (delivery["key"], delivery["kind"], delivery["project"]))
        self.assertIn(f"theseus merge {TASK}", delivery["command"])
        self.assertEqual(f"{TASK}: handoff delivered, waiting for review and merge", delivery["summary"])
        self.assertRegex(delivery["since"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(delivery, self.attention()["items"][0], "seeing an item never clears it or moves its time")
        self.theseus("merge", TASK)
        self.assertEqual([f"merged:{TASK}"], [entry["key"] for entry in self.attention()["items"]])
        self.assertIn("1 merge to verify", self.theseus("attention").stdout)


class PushTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.scaffold()
        self.state = self.tmp / "state"
        bin_dir = self.tmp / "bin"
        self.state.mkdir()
        bin_dir.mkdir()
        (bin_dir / "tmux").write_text(FAKE_TMUX)
        (bin_dir / "tmux").chmod(0o755)
        os.environ.update({"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_STATE": str(self.state),
                           "FAKE_PANES": "%9", "THESEUS_SEND_POLL": "0.01"})
        self.clock = fake_clock(self)
        from theseus import attention, config as cfg
        self.attention = attention
        self.conf = lambda: cfg.load(root=self.project)

    def typed_lines(self):
        path = self.state / "typed"
        lines = path.read_text().splitlines() if path.exists() else []
        return [line.split(" -l ", 1)[1] for line in lines if " -t %9 -l " in line]

    def configure(self, text):
        self.write("theseus.local.toml", text)

    def test_push_types_once_per_change_never_into_a_prompt_and_reminds_once(self):
        self.configure('[attention]\nnotify = "pane"\nremind_minutes = 10\nescalate_minutes = 0\n')
        self.assertIn("registered the coordinator of demo-port on pane %9", self.attention.register(self.conf(), "%9"))
        t0 = time.time()
        one = [item("delivery:a", "delivery", "delivery", t0)]
        two = one + [item("agent:w:stalled:b", "agent", "stalled agent", t0)]
        push = lambda found, minutes: self.attention.push(self.conf(), found, now=t0 + minutes * 60)

        self.assertEqual(["theseus: 1 item needs you (1 delivery); run theseus attention"], push(one, 0))
        self.assertEqual([], push(one, 1), "an unchanged list is never typed twice")
        (self.state / "screen-%9").write_text(" \u2502 Do you want to proceed?\n \u2502 \u276f 1. Yes\n \u2502   2. No\n")
        self.assertEqual([], push(two, 2), "nothing is typed while the pane shows a prompt")
        (self.state / "screen-%9").write_text("> half a sentence the user is typing\n")
        self.assertEqual([], push(two, 3), "nor over text in the input line")
        (self.state / "screen-%9").write_text("> theseus: 2 items need you (1 deliv\n")
        self.assertEqual(["theseus: 2 items need you (1 delivery, 1 stalled agent); run theseus attention"], push(two, 4),
                         "the change is typed once the pane is free, over an unsent line of its own")
        (self.state / "screen-%9").write_text("> \n")
        self.assertEqual([], push(one, 5), "a resolved item alone types nothing")
        self.assertEqual(["theseus: reminder: 1 item still needs you (1 delivery); run theseus attention"], push(one, 15))
        self.assertEqual([], push(one, 30), "one reminder only")
        self.assertEqual(3, len(self.typed_lines()), self.typed_lines())
        self.assertEqual("1 item needs you (1 delivery)", self.attention.summary(one))

    def test_escalation_runs_the_command_once_per_item_left_open(self):
        out = self.tmp / "escalated.txt"
        (self.tmp / "config.toml").write_text(f'[attention]\nescalate_command = "cat >> {out}"\n')
        self.configure("[attention]\nescalate_minutes = 30\n")
        t0 = time.time()
        old = item("delivery:a", "delivery", "delivery", t0 - 3600)
        young = item("merged:b", "merged", "merge to verify", t0)
        self.attention.push(self.conf(), [old, young], now=t0)
        self.attention.push(self.conf(), [old, young], now=t0 + 60)
        text = out.read_text()
        self.assertEqual(1, text.count("theseus demo-port:"), text)
        self.assertIn("delivery:a needs you", text)
        self.assertNotIn("merged:b", text)
        self.attention.push(self.conf(), [old, young], now=t0 + 31 * 60)
        self.assertEqual(2, out.read_text().count("theseus demo-port:"), "the item that aged since escalates once too")
        self.assertIn("merged:b", out.read_text())
        state = self.attention.console(self.conf(), [])
        self.assertEqual([], state["escalated"], "the console derives its banner from the live list")
