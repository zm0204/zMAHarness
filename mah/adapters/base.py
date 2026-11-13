"""The Adapter contract used by RunManager.dispatch and the per-Adapter policy flags.

Scope: this Protocol describes Adapters dispatched through ``RunManager.dispatch``.
``mah.fake_adapter.FakeAdapter`` is a legacy test double with its own ``dispatch``
flow and is deliberately outside this contract; new Adapters must not copy it.
"""
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class AdapterPolicy:
    """Named rules that the ledger, Run Manager and CLI apply per Adapter name.

    Every default is False/None, which reproduces the historical behavior for an
    Adapter name that has no registered policy (for example, an old ledger row).
    """

    # The name may appear in the local TOML (adapters.enabled and defaults.*.adapter).
    user_configurable: bool = False
    # Cards and Grants must bind timeout, worktree and task SHA-256, and a launch claim
    # must present a bound invocation.
    requires_dispatch_scope: bool = False
    # Outside codex_prompt, approval must be manual_cli with interactive terminal evidence.
    attended_approval_only: bool = False
    # The local Windows approval window may approve a card targeting only such Adapters.
    window_approval: bool = False
    # A FAILED structured-output conflict may be re-collected from the sealed receipt.
    replays_structured_output: bool = False
    # A process_not_started pre-submit refund is allowed with a bound capture hash.
    refunds_unstarted_process: bool = False
    # GUI submission: a LAUNCHING run may be cancelled with gui_not_submitted evidence.
    gui_submission: bool = False
    # Success comes only from the template Verifier; adoption totals are checked
    # against the verified result.
    template_verified: bool = False
    # Prefix for CLI-generated run IDs: "<prefix>-<uuid4 hex>".
    run_id_prefix: str | None = None


@runtime_checkable
class Adapter(Protocol):
    """A worker Adapter driven by ``RunManager.dispatch``.

    The Run Manager calls ``prepare_invocation`` before reserving the Grant,
    publishes the task, then calls ``launch``, ``observe``, ``persist_capture``
    and finally ``collect_saved``. The CLI may later call ``collect_saved`` again
    for ``resolve`` and ``collect``.
    """

    #: Adapter name stored in the ledger ``runs.adapter`` column and card targets.
    id: str
    #: The RunManager this instance is bound to; dispatch rejects any other manager.
    manager: Any

    def prepare_invocation(self, *, run_id, repo_root, template, model, effort, task, workspace,
                           worktree_root, timeout_s, requirements):
        """Validate inputs and build a fixed launch plan without starting anything.

        Called before the Grant is reserved. The returned plan must expose
        ``workspace_kind``, ``cwd`` and ``timeout_s``; these are bound in the
        reservation. Raise ``LedgerError`` to refuse; no ledger state is changed.
        """

    def launch(self, plan):
        """Claim the launch through ``manager.claim_launch`` and start the worker once.

        This is the only place a worker process or request may be started. Return a
        handle for ``observe``. ``OSError``/``ValueError`` before LAUNCHING cancel the run.
        """

    def observe(self, handle):
        """Wait for the worker to finish or reach the approved deadline; return a capture.

        Must not publish results. ``OSError``/``TimeoutExpired`` mark the run
        SUBMISSION_UNKNOWN and keep partial evidence.
        """

    def persist_capture(self, handle, capture):
        """Seal the observation into the run directory so recovery never relaunches."""

    def collect_saved(self, run_id, **options):
        """Re-read the sealed observation and drive the run to its collected state.

        Must never start a worker. Returns the run summary. Used by dispatch and by
        the CLI ``resolve`` and ``collect`` commands.
        """
