"""zMAHarness command line."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import shlex
import secrets
import sqlite3
import sys
from uuid import uuid4

from .adapters.agy_print import AgyPrintAdapter
from .config import _local_path, load_config
from .context_bundle import bundle_manifest, check_bundle, prepare_bundle, prepare_worktree
from .ledger import (BRAND_NAME, DISPATCH_SCOPE_FIELDS, READ_ONLY_TEMPLATES, SHA256, SUGGESTION_CARD_TITLE,
                     GrantLedger, LedgerError, TEMPLATE_TIMEOUTS, TEMPLATES, valid_id)
from .run_manager import RunManager
from .verifier import VerificationEvidence, Verifier

CARD_KEYS = {
    "card_id", "repo_root", "task_key", "trigger_ids", "prepare_mode",
    "allowed_templates", "allowed_targets", "max_dispatches", "ttl_minutes",
}


def parser():
    root = argparse.ArgumentParser(prog="mah", description=f"{BRAND_NAME} 派工與授權")
    root.add_argument("--config", type=Path, help="本機 TOML 設定檔")
    groups = root.add_subparsers(dest="group", required=True)
    bundle = groups.add_parser("bundle").add_subparsers(dest="action", required=True)
    bundle_create = bundle.add_parser("create")
    bundle_create.add_argument("--repo", type=Path, required=True)
    bundle_create.add_argument("--file", action="append", required=True)
    bundle_create.add_argument("--task-file", type=Path, required=True)
    bundle_create.add_argument("--output-dir", type=Path, required=True)
    bundle_check = bundle.add_parser("check")
    bundle_check.add_argument("--repo", type=Path, required=True)
    bundle_check.add_argument("--task-file", type=Path, required=True)
    worktree = groups.add_parser("worktree").add_subparsers(dest="action", required=True)
    worktree_create = worktree.add_parser("create", help="建立包含目前未提交內容的分離審查快照")
    worktree_create.add_argument("--repo", type=Path, required=True)
    worktree_create.add_argument("--run-id", required=True)
    card = groups.add_parser("card").add_subparsers(dest="action", required=True)
    create = card.add_parser("create")
    create.add_argument("--file", required=True, type=Path)
    create.add_argument("--override-suggestion-limit", action="store_true")
    for action in ("show", "reject"):
        card.add_parser(action).add_argument("card_id")
    grant = groups.add_parser("grant").add_subparsers(dest="action", required=True)
    approve = grant.add_parser("approve")
    approve.add_argument("card_id")
    approve.add_argument("--sha", required=True)
    approve.add_argument("--via", required=True, choices=("manual_cli", "codex_prompt"))
    window = grant.add_parser("approve-window", help="以本機 Windows 視窗核准單次合成派工")
    window.add_argument("card_id")
    window.add_argument("--sha", required=True)
    for action in ("show", "close"):
        grant.add_parser(action).add_argument("grant_id")
    grant.add_parser("list")
    dispatch = groups.add_parser("dispatch")
    dispatch.add_argument("--grant", required=True)
    dispatch.add_argument("--run-id")
    dispatch.add_argument("--template", choices=sorted(TEMPLATES), required=True)
    dispatch.add_argument("--adapter", choices=("agy-print",), required=True)
    dispatch.add_argument("--model", required=True)
    dispatch.add_argument("--effort", required=True)
    dispatch.add_argument("--task-file", required=True, type=Path)
    dispatch.add_argument("--workspace", required=True, type=Path)
    dispatch.add_argument("--timeout", type=int)
    dispatch.add_argument("--requirements-file", type=Path)
    dispatch.add_argument("--bundle-smoke", action="store_true", help=argparse.SUPPRESS)
    status = groups.add_parser("status")
    selector = status.add_mutually_exclusive_group(required=True)
    selector.add_argument("run_id", nargs="?")
    selector.add_argument("--active", action="store_true")
    collect = groups.add_parser("collect")
    collect.add_argument("run_id")
    collect.add_argument("--replay-output", action="store_true", help="僅用原始 agy 收據重收集結構化輸出衝突")
    verify = groups.add_parser("verify")
    verify.add_argument("run_id")
    verify.add_argument("--evidence-file", type=Path, help="Controller 獨立驗收證據（唯讀分析模板）")
    for action in ("accept", "reject-result"):
        adoption = groups.add_parser(action)
        adoption.add_argument("run_id")
        adoption.add_argument("--reason", required=True)
        if action == "accept":
            adoption.add_argument("--adopted", type=int)
            adoption.add_argument("--total", type=int)
    resolve = groups.add_parser("resolve")
    resolve.add_argument("run_id")
    actions = resolve.add_mutually_exclusive_group()
    for action in ("mark_failed", "abandon", "attach", "redispatch"):
        actions.add_argument("--" + action.replace("_", "-"), dest="resolution",
                             action="store_const", const=action)
    return root


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("派工卡 JSON 不能包含重複欄位")
        value[key] = item
    return value


def read_card(path, config):
    try:
        card = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("派工卡必須是有效的 UTF-8 JSON") from exc
    if type(card) is not dict or card.keys() - (CARD_KEYS | DISPATCH_SCOPE_FIELDS):
        raise ValueError("派工卡必須是符合規格的 JSON 物件")
    card.setdefault("ttl_minutes", config.ledger_limits.ttl_minutes)
    if missing := CARD_KEYS - card.keys():
        raise ValueError("派工卡缺少必要欄位：" + ", ".join(sorted(missing)))
    if "timeout_seconds" not in card and {"workspace_path", "task_sha256"} <= card.keys():
        templates = card["allowed_templates"]
        if not isinstance(templates, list) or not templates or any(
                not isinstance(template, str) or template not in TEMPLATE_TIMEOUTS for template in templates):
            raise ValueError("派工卡須指定有效模板才能套用預設逾時")
        card["timeout_seconds"] = min(TEMPLATE_TIMEOUTS[template] for template in templates)
    scope_keys = DISPATCH_SCOPE_FIELDS & card.keys()
    if scope_keys and (scope_keys != DISPATCH_SCOPE_FIELDS or any(card[key] is None for key in scope_keys)):
        raise ValueError("派工卡必須同時指定逾時、工作樹與任務 SHA-256")
    repo = card["repo_root"]
    if type(repo) is not str or not repo.strip() or any(ord(char) < 32 for char in repo):
        raise ValueError("repo_root 必須是單行路徑")
    return card


def show_card(card):
    content = json.loads(card["content_json"])
    targets = "；".join(f"{target['adapter']} / {target['model']} / {target['effort_max']}"
                       for target in content["allowed_targets"])
    print(f"{SUGGESTION_CARD_TITLE} {card['card_id']}　{card['status']}　觸發：{', '.join(content['trigger_ids'])}")
    print(f"範圍：{content['repo_root']}")
    print(f"模板：{', '.join(content['allowed_templates'])}　目標／等級上限：{targets}")
    print(f"上限：{content['max_dispatches']} 筆、併發 1、核准後有效 {content['ttl_minutes']} 分鐘")
    if "timeout_seconds" in content:
        print(f"逾時：{content['timeout_seconds']} 秒")
        print(f"工作樹：{content['workspace_path']}")
        print(f"任務 SHA-256 {content['task_sha256']}")
    print("驗收：由對應模板規定。額度：unknown。")
    print(f"預估影響：最多預留 {content['max_dispatches']} 筆派工。")
    print(f"SHA-256 {card['content_sha256']}")
    if card["status"] == "PROPOSED":
        print("核准：依已確認的使用者決定執行 mah grant approve。")


def grant_view(row):
    if row is None:
        raise LedgerError(2, "找不到 Grant")
    grant = dict(row)
    for key in ("allowed_templates", "allowed_targets"):
        grant[key] = json.loads(grant[key])
    scope = grant.pop("dispatch_scope_json")
    grant["dispatch_scope"] = json.loads(scope) if scope is not None else None
    return grant


def output(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def approve(ledger, args):
    evidence = None
    if args.via == "manual_cli":
        card = ledger.approval_candidate(args.card_id, args.sha, via=args.via)
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise LedgerError(10, "manual_cli 需要使用者直接操作互動終端")
        show_card(card)
        confirmation = f"APPROVE {card['card_id']} {secrets.token_hex(4).upper()}"
        try:
            answer = input(f"確認卡片、範圍與完整雜湊後，請親自輸入 {confirmation}：")
        except (EOFError, KeyboardInterrupt):
            raise LedgerError(10, "使用者未確認核准") from None
        if answer != confirmation:
            raise LedgerError(10, "使用者未確認核准")
        evidence = "interactive_terminal_confirmation"
    grant_id = ledger.approve(args.card_id, args.sha, via=args.via, evidence=evidence)
    output(grant_view(ledger.get_grant(grant_id)))


def dispatch_agy(manager, config, args):
    adapters = config.values["adapters"]
    if args.adapter not in adapters["enabled"]:
        raise LedgerError(14, "agy-print 尚未在本機設定啟用")
    grant = manager.ledger.get_grant(args.grant)
    if grant is None:
        raise LedgerError(10, "找不到有效 Grant")
    timeout = args.timeout
    if timeout is None and grant["dispatch_scope_json"] is not None:
        try:
            timeout = json.loads(grant["dispatch_scope_json"])["timeout_seconds"]
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerError(10, "Grant 缺少有效的核准逾時") from exc
    command = adapters["agy_command"]
    # A single executable path relative to the local TOML, or a command on PATH.
    if Path(command).is_absolute() or "/" in command or chr(92) in command:
        command = str((config.path.parent / Path(command)).resolve())
    requirements = None
    if args.requirements_file is not None:
        requirements = json.loads(args.requirements_file.read_text(encoding="utf-8-sig"),
                                  object_pairs_hook=unique_object)
    task = args.task_file.read_bytes()
    # Historical smoke commands still require a bundle; ordinary bundles need no switch.
    if args.bundle_smoke and bundle_manifest(task) is None:
        raise LedgerError(17, "--bundle-smoke 需要 Context-Bundle")
    adapter = AgyPrintAdapter(manager, command=command, max_task_bytes=config.values["limits"]["max_task_bytes"])
    return manager.dispatch(
        adapter, grant_id=args.grant, run_id=args.run_id or f"agy-{uuid4().hex}", repo_root=grant["repo_root"],
        template=args.template, model=args.model, effort=args.effort, task=task,
        workspace=args.workspace, worktree_root=config.values["paths"]["worktree_root"],
        timeout_s=timeout, requirements=requirements,
    )


def read_analysis_evidence(manager, config, run_id, path):
    run = manager.ledger.get_run(run_id)
    if run is None or run["template"] not in READ_ONLY_TEMPLATES:
        raise LedgerError(2, "Controller evidence file requires an existing read-only analysis run")
    if path.is_symlink():
        raise LedgerError(30, "Controller evidence cannot be a symbolic link")
    source = _local_path(path.resolve(strict=True), "Controller evidence")
    worker_roots = (manager.root, Path(run["repo_root"]), Path(run["workspace_path"]),
                    config.values["paths"]["worktree_root"], config.values["paths"]["gui_workspace_root"])
    if any(source.is_relative_to(root.resolve()) for root in worker_roots):
        raise LedgerError(30, "Controller evidence must stay outside worker and run directories")
    with source.open("rb") as file:
        raw = file.read(8193)
    if len(raw) > 8192:
        raise LedgerError(2, "Controller evidence file is too large")
    data = json.loads(raw, object_pairs_hook=unique_object)
    template = run["template"]
    extra = {"review": "reviewed_findings", "second-opinion": "experiments", "research": "checked_citations"}[template]
    required = {"run_id", "task_sha256", "result_sha256", "confirmed", "workspace_changed", extra}
    optional = {"bundle_manifest_sha256"} | ({"invalid_citation"} if template == "research" else set())
    if not isinstance(data, dict) or not required <= data.keys() or data.keys() - (required | optional):
        raise LedgerError(2, "Unexpected Controller evidence fields")
    if data.pop("run_id") != run_id:
        raise LedgerError(30, "Controller evidence belongs to a different run")
    if (any(not valid_id(SHA256, data[key]) for key in ("task_sha256", "result_sha256", "bundle_manifest_sha256")
            if key in data) or any(type(data[key]) is not bool for key in ("confirmed", "workspace_changed"))
            or type(data.get("invalid_citation", False)) is not bool):
        raise LedgerError(2, "Invalid Controller evidence values")
    items = data[extra]
    if template == "review":
        if type(items) is not int or items < 0:
            raise LedgerError(2, "reviewed_findings must be a non-negative integer")
    elif template == "second-opinion":
        if not isinstance(items, list) or any(not isinstance(item, str) or not item.strip() for item in items):
            raise LedgerError(2, "experiments must be a list of nonempty strings")
        data[extra] = tuple(items)
    else:
        if not isinstance(items, list) or any(
            not isinstance(item, list) or len(item) != 3
            or any(not isinstance(part, str) or not part.strip() for part in item) for item in items
        ):
            raise LedgerError(2, "checked_citations must contain path, lines and claim triples")
        data[extra] = tuple(tuple(item) for item in items)
    return VerificationEvidence(**data, source_sha256=sha256(raw).hexdigest())


def run_exit_code(run):
    if run["state"] == "SUBMISSION_UNKNOWN":
        return 21
    if run["state"] in ("FAILED", "FAILED_PRE_SUBMIT", "TIMED_OUT"):
        return 30 if run["state_reason"] == "schema_invalid" else 20
    return 0 if run["state"] == "SUCCEEDED" else 30


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
        for warning in config.warnings:
            print(f"警告：{warning}", file=sys.stderr)
        if args.group == "worktree":
            output(prepare_worktree(args.repo, config.values["paths"]["worktree_root"], args.run_id))
            return 0
        if args.group == "bundle":
            max_bytes = config.values["limits"]["max_task_bytes"]
            if args.action == "check":
                report = check_bundle(args.repo, args.task_file, max_bytes=max_bytes)
                output(report)
                return 0 if report["source_matches"] else 30
            directory = _local_path(args.output_dir.resolve(), "Bundle output")
            output(prepare_bundle(args.repo, args.file, args.task_file, directory,
                                  max_bytes=max_bytes))
            return 0
        card_content = read_card(args.file, config) if args.group == "card" and args.action == "create" else None
        config.database.parent.mkdir(parents=True, exist_ok=True)
        ledger = GrantLedger(config.database, limits=config.ledger_limits)
        manager = RunManager(ledger, max_summary_chars=config.values["limits"]["max_result_summary_chars"])
        manager.scan()
        if args.group == "dispatch":
            run = dispatch_agy(manager, config, args)
            output(run)
            return run_exit_code(run)
        if args.group == "status":
            output(manager.status(args.run_id))
        elif args.group == "resolve":
            run = ledger.get_run(args.run_id)
            if run is not None and run["adapter"] == "agy-print":
                AgyPrintAdapter(manager).collect_saved(args.run_id)
            output(manager.resolve(args.run_id, action=args.resolution))
        elif args.group in ("accept", "reject-result"):
            output(manager.record_acceptance(
                args.run_id, accepted=args.group == "accept", reason=args.reason,
                adopted=getattr(args, "adopted", None), total=getattr(args, "total", None),
            ))
        elif args.group in ("collect", "verify"):
            if args.group == "verify":
                prefixes = [shlex.split(prefix) for prefix in config.values["verify"]["allowed_command_prefixes"]]
                evidence = (read_analysis_evidence(manager, config, args.run_id, args.evidence_file)
                            if args.evidence_file is not None else None)
                collected = Verifier(manager, allowed_command_prefixes=prefixes).verify(args.run_id, evidence=evidence)
            else:
                run = ledger.get_run(args.run_id)
                if run is not None and run["adapter"] == "agy-print":
                    collected = AgyPrintAdapter(manager).collect_saved(args.run_id, replay_output=args.replay_output)
                elif args.replay_output:
                    raise LedgerError(20, "Output replay requires an existing agy-print run")
                else:
                    collected = manager.collect(args.run_id)
            output(collected)
            if (collected["state"] == "SUCCEEDED" and args.group == "verify"
                    and collected["verification"]["decision"] != "verified"):
                return 30
            return run_exit_code(collected)
        elif args.group == "card":
            if args.action == "create":
                ledger.create_card(**card_content, override_suggestion_limit=args.override_suggestion_limit,
                                   enabled_adapters=config.values["adapters"]["enabled"])
                args.card_id = card_content["card_id"]
            elif args.action == "reject":
                ledger.reject_card(args.card_id)
            card = ledger.get_card(args.card_id)
            if card is None:
                raise LedgerError(2, "找不到派工卡")
            show_card(card)
        elif args.action == "approve":
            approve(ledger, args)
        elif args.action == "approve-window":
            from .window_approval import review_window

            grant_id = review_window(ledger, args.card_id, args.sha)
            output(grant_view(ledger.get_grant(grant_id)))
        elif args.action == "list":
            output([grant_view(row) for row in ledger.list_grants()])
        else:
            if args.action == "close":
                ledger.close(args.grant_id)
            output(grant_view(ledger.get_grant(args.grant_id)))
        return 0
    except LedgerError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return exc.code
    except (OSError, ValueError, TypeError, OverflowError, sqlite3.Error) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2
