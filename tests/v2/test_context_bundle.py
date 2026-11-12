from hashlib import sha256
import os
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from mah import context_bundle
from mah.context_bundle import HEADER, bundle_manifest, prepare_bundle
from mah.fake_adapter import FakeAdapter
from mah.ledger import GrantLedger, LedgerError, dump
from mah.run_manager import RunManager


ROOT = Path(__file__).resolve().parents[2]
FILES = ("setup.ps1", "config/defaults.json")


class ContextBundleTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory(prefix="MAH Bundle 中文 ", dir=ROOT.parent)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "source"
        subprocess.run(["git", "init", "--quiet", str(self.repo)], check=True, timeout=10)
        objects = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute",
                                           "--git-path", "objects"], timeout=10).strip()
        (self.repo / ".git/objects/info/alternates").write_bytes(objects + b"\n")
        head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], timeout=10).decode().strip()
        self.git("update-ref", "HEAD", head)
        self.git("read-tree", "HEAD")
        self.git("checkout-index", "--all")
        self.instructions = self.root / "instructions.md"
        self.instructions.write_text("請只審查提供的內容。", encoding="utf-8")
        self.settings = self.root / "harness.local.toml"
        self.settings.write_text('[paths]\nstate_dir = "state"\n', encoding="utf-8")
        self.state = self.root / "state"
        self.bundle = self.root / "bundle"

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), "-c", "core.autocrlf=false", *args],
                                       timeout=15).strip()

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "mah", "--config", str(self.settings), *args],
                              cwd=ROOT, capture_output=True, encoding="utf-8", timeout=15)

    def create(self, *paths):
        args = [item for path in paths for item in ("--file", path)]
        return self.cli("bundle", "create", "--repo", str(self.repo), "--task-file", str(self.instructions),
                        "--output-dir", str(self.bundle), *args)

    def dispatch(self, *, finding_file=FILES[0], required_tools=(), scenario="success", task=None):
        if task is None:
            outcome = self.create(*FILES)
            self.assertEqual(outcome.returncode, 0, outcome.stderr)
            task = (self.bundle / "task.md").read_bytes()
        self.state.mkdir(exist_ok=True)
        ledger = GrantLedger(self.state / "ledger.sqlite3")
        card = ledger.create_card(
            "offline-bundle", repo_root=self.repo, task_key="offline-bundle", trigger_ids=["T5"],
            prepare_mode="explicit", allowed_templates=["review"],
            allowed_targets=[{"adapter":"fake","model":"synthetic","effort_max":"low"}],
            max_dispatches=1, ttl_minutes=10,
        )
        # Test fixture only. No interactive approval and no real Adapter.
        grant = ledger.approve("offline-bundle", card, via="manual_cli")
        run = f"bundle-{uuid4().hex}"
        result = {"findings":[{"severity":"minor","file":finding_file,"line":1,"title":"合成發現",
                               "rationale":"合成理由","suggestion":"合成建議"}]}
        FakeAdapter(scenario=scenario,payload=result).dispatch(
            RunManager(ledger),grant,run,repo_root=self.repo,template="review",model="synthetic",
            effort="low",task=task,requirements={"required_tools":list(required_tools)},
        )
        return ledger, grant, run

    def evidence(self, ledger, run):
        directory = self.state / "runs" / run
        return {"run_id":run,"task_sha256":ledger.get_run(run)["task_sha256"],
                "result_sha256":sha256((directory/"result.json").read_bytes()).hexdigest(),
                "bundle_manifest_sha256":sha256((self.bundle/"manifest.json").read_bytes()).hexdigest(),
                "confirmed":True,"workspace_changed":False,"reviewed_findings":1}

    def verify(self, run, evidence, *, path=None):
        path = path or self.root/"controller-evidence.json"
        path.write_text(json.dumps(evidence),encoding="utf-8")
        return self.cli("verify",run,"--evidence-file",str(path))

    def test_manifest_and_task_are_sealed_from_explicit_clean_head_files(self):
        proc = self.create(*FILES)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        manifest = (self.bundle / "manifest.json").read_bytes()
        data = json.loads(manifest)
        self.assertEqual(data["head"], self.git("rev-parse", "HEAD").decode())
        self.assertEqual([item["path"] for item in data["files"]], sorted(FILES))
        for item in data["files"]:
            raw = (self.repo / item["path"]).read_bytes()
            self.assertEqual(item["sha256"], sha256(raw).hexdigest())
            self.assertEqual(item["size_bytes"], len(raw))
        task = (self.bundle / "task.md").read_bytes()
        self.assertIn("從第 1 行開始".encode(), task)
        self.assertEqual(json.loads(task[len(HEADER):])["files"],
                         {name: (self.repo / name).read_bytes().decode("utf-8") for name in FILES})
        self.assertEqual(json.loads(proc.stdout)["task_sha256"], sha256(task).hexdigest())
        self.assertIn(sha256(manifest).hexdigest().encode(), task)
        self.assertNotEqual(self.create(*FILES).returncode, 0)
        self.assertEqual((self.bundle / "task.md").read_bytes(), task)

    def test_worktree_cli_creates_a_clean_linked_detached_head_without_commit(self):
        run_id = f"review-{uuid4().hex}"
        proc = self.cli("worktree", "create", "--repo", str(self.repo), "--run-id", run_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        workspace = Path(report["worktree_path"])
        self.assertEqual(workspace, self.root / "worktrees" / run_id)
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(report["snapshot_head"], head.decode())
        self.assertEqual(report["baseline_sha256"], sha256(head + b"\0").hexdigest())
        self.assertEqual(subprocess.run(["git", "-C", str(workspace), "symbolic-ref", "--quiet", "HEAD"],
                                        capture_output=True, timeout=15).returncode, 1)
        self.assertFalse(self.state.exists())
        self.assertNotEqual(self.cli("worktree", "create", "--repo", str(self.repo), "--run-id", run_id).returncode, 0)

    def test_dirty_worktree_stages_effective_bytes_in_detached_tree_only(self):
        added = "新增程式.py"
        self.allow(added, ".env", "ignored.pem")
        (self.repo / added).write_bytes(b"# new module\n" + b"# code\n" * 6000)
        (self.repo / ".env").write_bytes(b"synthetic fixture\n")
        (self.repo / "ignored.pem").write_bytes(b"synthetic fixture\n")
        (self.repo / "ignored.txt").write_bytes(b"ignored\n")
        (self.repo / FILES[0]).write_bytes(b"staged\n")
        self.git("add", "--", FILES[0])
        (self.repo / FILES[0]).write_bytes(b"effective worktree contents\n")
        self.git("rm", "--", FILES[1])
        self.git("config", "core.filemode", "false")
        self.git("update-index", "--chmod=+x", "--", "README.md")
        source_head = self.git("rev-parse", "HEAD")
        source_branch = self.git("symbolic-ref", "HEAD")
        source_status = self.git("status", "--porcelain=v1", "-z", "--untracked-files=all")
        index = (self.repo / ".git/index").read_bytes()
        run_id = f"review-{uuid4().hex}"
        workspace = self.root / "worktrees" / run_id
        git = context_bundle._git
        simulated = []

        def intercept(cwd, *args, **kwargs):
            if args[0] == "commit":
                self.assertEqual(cwd, workspace)
                self.assertIn("--no-verify", args)
                self.assertIn("--no-gpg-sign", args)
                self.assertIn(f"mah-snapshot:{run_id}", args)
                self.assertIn("user.name=MultiAgentHarness", kwargs["settings"])
                self.assertEqual(git(cwd, "show", ":" + FILES[0]), (self.repo / FILES[0]).read_bytes())
                self.assertEqual(git(cwd, "show", ":" + added), (self.repo / added).read_bytes())
                self.assertTrue(git(cwd, "ls-files", "--stage", "--", "README.md").startswith(b"100755 "))
                self.assertFalse((cwd / FILES[1]).exists())
                ignore_at_head = git(self.repo, "show", "HEAD:.gitignore")
                self.assertEqual(git(cwd, "show", ":.gitignore"), ignore_at_head)
                # Git may convert line endings when populating the linked worktree.
                self.assertEqual((cwd / ".gitignore").read_bytes().replace(b"\r\n", b"\n"),
                                 ignore_at_head.replace(b"\r\n", b"\n"))
                self.assertFalse(any((cwd / name).exists() for name in (".env", "ignored.pem", "ignored.txt")))
                simulated.append(args)
                return b""
            if simulated and cwd == workspace and args[:3] == ("rev-parse", "--verify", "HEAD"):
                return b"a" * 40 + b"\n"
            return git(cwd, *args, **kwargs)

        with patch.object(context_bundle, "_git", side_effect=intercept), patch.object(
                context_bundle, "_worktree_baseline", return_value="b" * 64):
            report = context_bundle.prepare_worktree(self.repo, workspace.parent, run_id)
        self.assertEqual(len(simulated), 1)
        self.assertEqual(report, {"worktree_path": str(workspace), "snapshot_head": "a" * 40,
                                  "baseline_sha256": "b" * 64})
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.assertEqual(self.git("rev-parse", "HEAD"), source_head)
        self.assertEqual(self.git("symbolic-ref", "HEAD"), source_branch)
        self.assertEqual(self.git("status", "--porcelain=v1", "-z", "--untracked-files=all"), source_status)

    def test_worktree_refuses_to_copy_a_hardlink_and_does_not_publish(self):
        self.allow("alias.py")
        try:
            os.link(self.instructions, self.repo / "alias.py")
        except OSError:
            self.skipTest("This process cannot create hardlinks")
        run_id = f"review-{uuid4().hex}"
        proc = self.cli("worktree", "create", "--repo", str(self.repo), "--run-id", run_id)
        self.assertEqual(proc.returncode, 20, proc.stderr)
        self.assertFalse((self.root / "worktrees" / run_id).exists())

    def test_bundle_is_deterministic_and_keeps_unselected_local_changes_out(self):
        self.assertEqual(self.create(*FILES).returncode, 0)
        initial = self.bundle
        (self.repo / "README.md").write_bytes(b"UNSELECTED")
        self.bundle = self.root / "second-bundle"
        self.assertEqual(self.create(*reversed(FILES)).returncode, 0)
        for name in ("task.md", "manifest.json"):
            self.assertEqual((initial/name).read_bytes(), (self.bundle/name).read_bytes())
        self.assertEqual((self.repo / "README.md").read_bytes(), b"UNSELECTED")

    def test_manipulated_bundle_payload_is_rejected_before_launch(self):
        self.assertEqual(self.create(*FILES).returncode, 0)
        task = (self.bundle / "task.md").read_bytes()
        payload = json.loads(task[len(HEADER):])
        payload["files"][FILES[0]] += "altered"
        bad = HEADER + json.dumps(payload, ensure_ascii=False).encode()
        with self.assertRaisesRegex(LedgerError, "Context-Bundle integrity"):
            bundle_manifest(bad)
        with self.assertRaisesRegex(LedgerError, "Context-Bundle integrity"):
            self.dispatch(task=bad)

    def test_cli_evidence_closes_collected_review_but_not_adoption(self):
        ledger, grant, run = self.dispatch()
        pending = self.cli("verify", run)
        self.assertEqual(pending.returncode, 30, pending.stderr)
        self.assertEqual(json.loads(pending.stdout)["verification"]["reason"], "controller_evidence_missing")
        evidence = self.evidence(ledger,run)
        verified = self.verify(run,evidence)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        view = json.loads(verified.stdout)
        self.assertEqual((view["state"],view["acceptance_status"]),("SUCCEEDED","PENDING"))
        self.assertEqual(view["verification"]["bundle_manifest_sha256"],evidence["bundle_manifest_sha256"])
        receipt = (self.state/"runs"/run/"verify.json").read_bytes()
        self.assertEqual(self.verify(run,evidence).returncode, 0)
        self.assertEqual((self.state/"runs"/run/"verify.json").read_bytes(), receipt)
        self.assertEqual(ledger.get_grant(grant)["used_dispatches"], 1)

    def allow(self, *paths):
        with (self.repo / ".gitignore").open("ab") as file:
            for path in paths:
                file.write(("\n!/" + path + "\n").encode("utf-8"))

    def check(self):
        return self.cli("bundle", "check", "--repo", str(self.repo),
                        "--task-file", str(self.bundle / "task.md"))

    def test_snapshots_working_bytes_and_git_state_without_mutating_source(self):
        new = "新增.py"
        self.allow(new)
        (self.repo / new).write_bytes("# 新檔\r\n".encode())
        (self.repo / FILES[0]).write_bytes(b"# staged\n")
        self.git("add", "--", FILES[0])
        (self.repo / FILES[0]).write_bytes(b"# worktree\r\n")
        (self.repo / FILES[1]).write_bytes(b'{"worktree":true}\n')
        index = self.repo / ".git/index"
        paths = (*FILES, new)
        head = self.git("rev-parse", "HEAD")
        refs = self.git("show-ref")
        index_bytes = index.read_bytes()
        current = {path: (self.repo / path).read_bytes() for path in paths}

        proc = self.create(*paths)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        task = (self.bundle / "task.md").read_bytes()
        manifest, digest = bundle_manifest(task)
        self.assertEqual((manifest["version"], manifest["source"]), (2, "git_worktree"))
        self.assertEqual(json.loads(proc.stdout)["manifest_sha256"], digest)
        self.assertEqual(json.loads(proc.stdout)["task_sha256"], sha256(task).hexdigest())
        body = json.loads(task[len(HEADER):])
        core = {key: value for key, value in manifest.items() if key != "bundle_sha256"}
        whole = (dump({"instructions": body["instructions"], "manifest": core}) + "\n").encode()
        self.assertEqual(manifest["bundle_sha256"], sha256(whole).hexdigest())
        self.assertEqual(json.loads(proc.stdout)["bundle_sha256"], manifest["bundle_sha256"])
        entries = {item["path"]: item for item in manifest["files"]}
        for path in paths:
            self.assertEqual(body["files"][path].encode(), current[path])
            self.assertEqual(entries[path]["sha256"], sha256(current[path]).hexdigest())
        self.assertEqual(entries[FILES[0]]["source_status"],
                         {"staged": True, "worktree_modified": True, "untracked": False})
        self.assertEqual(entries[FILES[1]]["source_status"],
                         {"staged": False, "worktree_modified": True, "untracked": False})
        self.assertEqual(entries[new]["source_status"],
                         {"staged": False, "worktree_modified": False, "untracked": True})
        proc = self.check()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["source_matches"])
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("show-ref"), refs)
        self.assertEqual(index.read_bytes(), index_bytes)
        self.assertEqual({path: (self.repo / path).read_bytes() for path in paths}, current)

    def test_v2_integrity_covers_instructions_and_git_metadata(self):
        proc = self.create(*FILES)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        task = (self.bundle / "task.md").read_bytes()
        for change in ("instructions", "index"):
            with self.subTest(change=change):
                body = json.loads(task[len(HEADER):])
                if change == "instructions":
                    body[change] += " altered"
                else:
                    body["manifest"]["files"][0]["index"]["oid"] = "0" * 40
                body["manifest_sha256"] = sha256((dump(body["manifest"]) + "\n").encode()).hexdigest()
                with self.assertRaisesRegex(LedgerError, "Context-Bundle integrity"):
                    bundle_manifest(HEADER + (dump(body) + "\n").encode())

    def test_staged_new_file_captures_bytes_and_is_not_untracked(self):
        new = "new.py"
        self.allow(new)
        (self.repo / new).write_bytes(b"print('new')\n")
        self.git("add", "--", new)
        proc = self.create(new)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry, = json.loads((self.bundle / "manifest.json").read_bytes())["files"]
        self.assertIsNone(entry["head"])
        self.assertEqual(entry["source_status"], {"staged": True, "worktree_modified": False, "untracked": False})

    def test_check_detects_file_and_index_drift_with_no_bundle_rewrite(self):
        self.assertEqual(self.create(*FILES).returncode, 0)
        task = (self.bundle / "task.md").read_bytes()
        a, b = (self.repo / path for path in FILES)
        original = a.read_bytes()
        a.write_bytes(b"# staged\n")
        self.git("add", "--", FILES[0])
        a.write_bytes(original)
        b.write_bytes(b'{"changed":true}\n')
        proc = self.check()
        self.assertEqual(proc.returncode, 30, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertFalse(result["source_matches"])
        self.assertTrue(result["head_matches"])
        self.assertEqual(result["changed_files"], [
            {"path": FILES[1], "reasons": ["content_changed"]},
            {"path": FILES[0], "reasons": ["git_state_changed"]},
        ])
        b.unlink()
        self.assertIn({"path": FILES[1], "reasons": ["source_unavailable"]},
                      json.loads(self.check().stdout)["changed_files"])
        self.assertEqual((self.bundle / "task.md").read_bytes(), task)

    def test_unsafe_or_sensitive_selections_fail_before_publication(self):
        files = {".env": b"A=B", "harness.local.toml": b"private", "ignored.py": b"print(1)",
                 "safe.py": b'key = "-----BEGIN PRIVATE KEY-----"', "binary.py": b"before\0after",
                 "ordinary.py": b'api_key = "synthetic-sensitive-value"'}
        self.allow(*(name for name in files if name != "ignored.py"))
        for name, raw in files.items():
            (self.repo / name).write_bytes(raw)
        for name in files:
            with self.subTest(name=name):
                self.assertNotEqual(self.create(name).returncode, 0)
                self.assertFalse(self.bundle.exists())

    def test_hardlink_is_blocked(self):
        alias = self.repo / "hardlink.py"
        self.allow(alias.name)
        try:
            os.link(self.repo / FILES[0], alias)
        except PermissionError:
            self.skipTest("This test process cannot create hardlinks")
        proc = self.create(alias.name)
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.bundle.exists())

    def test_symlink_to_outside_file_is_blocked(self):
        alias = self.repo / "alias.py"
        self.allow(alias.name)
        try:
            alias.symlink_to(self.instructions)
        except PermissionError:
            self.skipTest("This test process cannot create symlinks")
        self.assertNotEqual(self.create(alias.name).returncode, 0)
        self.assertFalse(self.bundle.exists())

    @unittest.skipUnless(os.name == "nt", "Windows Junction only")
    def test_junction_to_outside_directory_is_blocked(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "example.py").write_text("print(1)", encoding="utf-8")
        alias = self.repo / "junction"
        self.allow(alias.name, "junction/example.py")
        link = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(outside)],
                              capture_output=True, timeout=10)
        if link.returncode:
            self.skipTest("This test process cannot create Windows Junctions")
        self.assertNotEqual(self.create("junction/example.py").returncode, 0)
        self.assertFalse(self.bundle.exists())

    def test_source_mutating_after_first_read_refuses_to_publish(self):
        read = context_bundle._read_worktree
        selected = self.repo / FILES[0]

        def mutate(*args, **kwargs):
            captured = read(*args, **kwargs)
            selected.write_bytes(b"# changed while packaging\n")
            return captured

        with patch.object(context_bundle, "_read_worktree", side_effect=mutate):
            with self.assertRaisesRegex(LedgerError, "changed during"):
                prepare_bundle(self.repo, [FILES[0]], self.instructions, self.bundle, max_bytes=32768)
        self.assertFalse(self.bundle.exists())

    def test_index_mutating_during_snapshot_refuses_to_publish(self):
        selected = self.repo / FILES[0]
        selected.write_bytes(b"# staged during capture\n")
        read = context_bundle._read_worktree

        def stage(*args, **kwargs):
            captured = read(*args, **kwargs)
            self.git("add", "--", FILES[0])
            return captured

        with patch.object(context_bundle, "_read_worktree", side_effect=stage):
            with self.assertRaisesRegex(LedgerError, "selected source state changed"):
                prepare_bundle(self.repo, [FILES[0]], self.instructions, self.bundle, max_bytes=32768)
        self.assertFalse(self.bundle.exists())

    def test_dirty_snapshot_verifies_independently_of_later_source_changes(self):
        selected = self.repo / FILES[0]
        selected.write_bytes(b"# uncommitted review subject\n")
        ledger, _, run = self.dispatch()
        selected.write_bytes(b"# newer working version\n")
        self.assertEqual(self.check().returncode, 30)
        self.assertEqual(self.cli("verify", run).returncode, 30)
        self.assertEqual(self.verify(run, self.evidence(ledger, run)).returncode, 0)
        self.assertEqual(ledger.get_run(run)["acceptance_status"], "PENDING")

    def test_output_directory_cannot_be_inside_source(self):
        self.bundle = self.repo / "bundle"
        self.assertNotEqual(self.create(*FILES).returncode, 0)
        self.assertFalse(self.bundle.exists())
        with self.assertRaisesRegex(LedgerError, "outside Git"):
            prepare_bundle(self.repo, FILES, self.instructions, self.bundle, max_bytes=32768)
        self.assertFalse(self.bundle.exists())

    def test_legacy_bundle_remains_valid_for_review(self):
        self.assertEqual(self.create(*FILES).returncode, 0)
        body = json.loads((self.bundle / "task.md").read_bytes()[len(HEADER):])
        manifest = body["manifest"]
        manifest.update(version=1, source="git_head")
        manifest.pop("bundle_sha256")
        manifest["files"] = [{key: item[key] for key in ("path", "size_bytes", "sha256", "line_count")}
                             for item in manifest["files"]]
        body["manifest_sha256"] = sha256((dump(manifest) + "\n").encode()).hexdigest()
        task = HEADER + (dump(body) + "\n").encode()
        self.assertEqual(bundle_manifest(task), (manifest, body["manifest_sha256"]))
        ledger, _, run = self.dispatch(task=task)
        evidence = self.evidence(ledger, run) | {"bundle_manifest_sha256": body["manifest_sha256"]}
        self.assertEqual(self.verify(run, evidence).returncode, 0)

    def test_invalid_path_and_case_collisions_are_rejected(self):
        for paths in (("../outside",),(".git/config",),("config/defaults.json:stream",),
                      (FILES[0],FILES[0].upper())):
            with self.subTest(paths=paths):
                self.assertNotEqual(self.create(*paths).returncode,0)
                self.assertFalse(self.bundle.exists())

    def test_wrong_evidence_or_worker_workspace_cannot_verify(self):
        ledger, _, run = self.dispatch()
        evidence = self.evidence(ledger,run)
        for key in ("run_id","task_sha256","result_sha256","bundle_manifest_sha256"):
            with self.subTest(key=key):
                self.assertEqual(self.verify(run,evidence | {key:"0"*64}).returncode,30)
        path=Path(ledger.get_run(run)["workspace_path"])/"worker-evidence.json"
        self.assertNotEqual(self.verify(run,evidence,path=path).returncode,0)
        self.assertEqual(ledger.get_run(run)["state"],"COLLECTING")

    def test_findings_outside_bundle_are_rejected(self):
        ledger, _, run = self.dispatch(finding_file="private.txt")
        result=self.verify(run,self.evidence(ledger,run))
        self.assertNotEqual(result.returncode,0,result.stderr)
        self.assertEqual(ledger.get_run(run)["state"],"FAILED")
        self.assertEqual(ledger.get_run(run)["state_reason"],"bundle_finding_out_of_scope")

    def test_review_evidence_for_an_unbundled_task_still_uses_existing_rules(self):
        ledger, grant, run = self.dispatch(task=b"Unbundled review")
        result = (self.state/"runs"/run/"result.json").read_bytes()
        proof = {"run_id":run,"task_sha256":ledger.get_run(run)["task_sha256"],
                 "result_sha256":sha256(result).hexdigest(),"confirmed":True,
                 "workspace_changed":False,"reviewed_findings":1}
        proc = self.verify(run,proof)
        self.assertEqual(proc.returncode,0,proc.stderr)
        self.assertEqual(ledger.get_run(run)["acceptance_status"],"PENDING")
        self.assertEqual(ledger.get_grant(grant)["used_dispatches"],1)

    def test_missing_required_tool_is_not_overridden_by_controller(self):
        ledger, _, run = self.dispatch(required_tools=("read-fixture",), scenario="missing_tool")
        proc=self.verify(run,self.evidence(ledger,run))
        self.assertEqual(proc.returncode,30,proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["verification"]["reason"],"required_tool_missing")
        self.assertEqual(ledger.get_run(run)["state"],"COLLECTING")
