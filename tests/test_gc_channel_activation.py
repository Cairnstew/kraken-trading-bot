"""Structural guards for the news + social channel activation (AC5/AC6/AC8).

`tests/test_rl_data_store.py` proves the merge seam carries the six new
signal columns.  What it cannot reach is the half that decides whether
anything ever *feeds* the seam: the two config keys, the two
`systemd/user` timer pairs, and the justfile recipes that install them.

The property under test is not "the file exists" — it is that the schedule
is *correct and non-colliding*.  Three producers all hitting the network on
the same minute is a real failure mode here: the funding timer's
`RandomizedDelaySec=120` makes it own 17:00–17:02, so a second unit on
`:17` silently contends for the same window and neither is wrong on its own.

So the OnCalendar lines are PARSED, not string-matched: ``*:17:00`` and
``:17:00`` are the same schedule and only the parse knows that.  Likewise
AC6 asserts on the *generated* `.service`, the `.in` after substitution,
because that is the file systemd actually reads — the template's
`@OUTPUT@` placeholder says nothing about where the bytes land.

No timer is fired and no network is touched: this is all static reading of
files that are committed.
"""

from __future__ import annotations

import ast
import inspect
import re
import shlex
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]

# The three channel timers, with the minute each one is *required* to own.
# Funding is included rather than assumed: it is the incumbent, it is the one
# with jitter, and a future edit that moves it must move these two too — which
# is exactly what the collision test below is for.
_TIMERS = {
    "kraken-trading-bot-funding.timer": 17,
    "kraken-trading-bot-news.timer": 23,
    "kraken-trading-bot-social.timer": 29,
}

# The two units this change adds, and the config key each one feeds.
_NEW_UNITS = {
    "kraken-trading-bot-news.service.in": "extra_features_file",
    "kraken-trading-bot-social.service.in": "social_features_file",
}

_ON_CALENDAR = re.compile(r"^\s*OnCalendar=(.+?)\s*$", re.M)


def _parse_timer_minute(text: str) -> int:
    """The minute-of-hour a systemd ``OnCalendar=`` line fires on.

    Parsed rather than grepped because the valid spellings are several
    (``*-*-* *:17:00``, ``*:17:00``, ``hourly``-style macros resolve away)
    and only the structured read is stable across them.  Anything that is not
    a recognisable daily minute-of-hour shape is an error, not a default —
    a timer whose schedule this guard cannot read is a timer it cannot
    vouch for.
    """
    match = _ON_CALENDAR.search(text)
    assert match, "no OnCalendar= line; a timer without one has no schedule"
    spec = match.group(1).strip()
    parsed: int | None = None
    try:
        # Best: let systemd itself resolve the spec.  `*-*-* *:17:00`,
        # `*:17:00` and `daily` all reduce to a concrete minute this way, so
        # a future edit to a more readable spelling cannot silently stop
        # being checked.  Not installed in this repo's dev shell, which is
        # why the regex fallback below is not optional.
        import systemd.calendar  # type: ignore[import-not-found]

        entries = systemd.calendar.CalendarSpecification(spec)
        assert len(entries) == 1, f"{spec!r} expands to {len(entries)} events, not one"
        parsed = int(entries[0].minute)  # type: ignore[union-attr]
    except ImportError:
        tail = re.search(r":(\d{1,2}):\d{2}\s*$", spec)
        parsed = int(tail.group(1)) if tail else None
    assert parsed is not None, (
        f"OnCalendar={spec!r} carries no readable minute-of-hour; this guard "
        "will not guess one"
    )
    return parsed


def _generate_service(unit: str, ticker: str, output: Path) -> str:
    """Substitute a ``.service.in`` the way the justfile recipe does.

    Same two ``sed`` replacements as ``just news-timer`` /
    ``just social-timer`` / ``just funding-timer``, so what is asserted on is
    byte-for-byte the file that lands in ``~/.config/systemd/user/``.
    """
    template = (_REPO / "systemd" / unit).read_text(encoding="utf-8")
    return template.replace("@TICKER@", ticker).replace("@PAIR@", ticker).replace(
        "@OUTPUT@", str(output)
    )


def _exec_start_args(service: str) -> list[str]:
    """The argv of the generated unit's ExecStart, shlex-split.

    Shlex rather than ``str.split`` because the path is unquoted and may hold
    a space; a naive split would silently compare the wrong token and pass.
    """
    line = next(
        ln for ln in service.splitlines() if ln.startswith("ExecStart=")
    )
    return shlex.split(line[len("ExecStart=") :])


# ---------------------------------------------------------------------------
# AC5 — both timers exist, on the right minutes, with catch-up
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("unit,minute", sorted(_TIMERS.items()))
def test_each_channel_timer_fires_on_its_declared_minute(unit: str, minute: int) -> None:
    """AC5: news at :23, social at :29, funding at :17 — on the parsed value."""
    path = _REPO / "systemd" / unit
    assert path.is_file(), f"{unit} is missing: the channel has no schedule"
    found = _parse_timer_minute(path.read_text(encoding="utf-8"))
    assert found == minute, (
        f"{unit} fires at minute {found}, not {minute}; the config comment "
        f"and the funding timer's jitter window are both computed from {minute}"
    )


@pytest.mark.parametrize("unit", sorted(_TIMERS))
def test_each_timer_is_persistent(unit: str) -> None:
    """`Persistent=true` on every one.

    Without it a suspend silently produces a gap bounded only by
    ``signal_max_age_hours``, and the file is forward-only so the missing
    hours are unrecoverable.  Checked on the parsed boolean, and asserted as
    exactly ``true`` because systemd also accepts ``1``, ``yes`` and
    ``True``.
    """
    text = (_REPO / "systemd" / unit).read_text(encoding="utf-8")
    values = re.findall(r"^\s*Persistent=(\S+)\s*$", text, re.M)
    assert values, f"{unit} has no Persistent= line"
    for value in values:
        assert value.lower() in {"true", "1", "yes"}, (
            f"{unit} sets Persistent={value}, which systemd reads as false — "
            "a missed hour would then be skipped silently"
        )


@pytest.mark.parametrize("unit", sorted(_TIMERS))
def test_no_timer_uses_onunitactivesec(unit: str) -> None:
    """`OnUnitActiveSec=` is banned here, and this is why.

    systemd 262's own timer(5): `Persistent=` is **silently inert** on a
    monotonic timer.  A schedule written that way therefore *looks* like it
    catches up after a suspend and does not, with no warning anywhere — and
    monotonic timers additionally drift off the UTC bar grid, so the pull
    would stop landing on the hour its own records are floored to.

    A guard that only asserted "the timer exists" would pass straight through
    this, which is the point of asserting its absence.
    """
    text = (_REPO / "systemd" / unit).read_text(encoding="utf-8")
    for banned in ("OnUnitActiveSec=", "OnBootSec=", "OnStartupSec="):
        assert banned not in text, (
            f"{unit} sets {banned}: Persistent= is silently inert on a "
            "monotonic timer, so a suspend would skip hours with no "
            "diagnostic. Use OnCalendar= + Persistent=true."
        )


def test_the_three_timers_never_share_a_minute() -> None:
    """AC5's collision half: no two of the three producers fire together.

    Names both units and the shared minute on failure, because "two timers
    collide" without saying *which two* and *when* is a note, not a
    diagnostic.  Spacing matters for a second reason beyond tidiness: the
    funding timer carries ``RandomizedDelaySec=120``, so it owns a two-minute
    window and a neighbour at :19 would already be inside it.
    """
    by_minute: dict[int, list[str]] = {}
    for unit in sorted(_TIMERS):
        by_minute.setdefault(_parse_timer_minute(
            (_REPO / "systemd" / unit).read_text(encoding="utf-8")
        ), []).append(unit)

    collisions = {
        minute: units for minute, units in by_minute.items() if len(units) > 1
    }
    assert not collisions, "timers share an OnCalendar minute: " + "; ".join(
        f"minute {minute}: {', '.join(units)}" for minute, units in collisions.items()
    )

    # ...and, given the funding timer's jitter, the gap has to exceed it.
    jitter = re.search(
        r"^\s*RandomizedDelaySec=(\d+)",
        (_REPO / "systemd" / "kraken-trading-bot-funding.timer").read_text(
            encoding="utf-8"
        ),
        re.M,
    )
    minutes = sorted(by_minute)
    gaps = [b - a for a, b in zip(minutes, minutes[1:])]
    if jitter:
        assert min(gaps) * 60 > int(jitter.group(1)), (
            f"adjacent timers are {min(gaps)} minute(s) apart but funding "
            f"carries RandomizedDelaySec={jitter.group(1)}, so they can still "
            "land in the same window"
        )


def test_every_timer_names_a_service_that_exists() -> None:
    """A timer pointing at a missing unit fails silently at fire time."""
    for unit in sorted(_TIMERS):
        text = (_REPO / "systemd" / unit).read_text(encoding="utf-8")
        named = re.search(r"^\s*Unit=(\S+)\s*$", text, re.M)
        assert named, f"{unit} has no Unit= line, so it defaults to the timer's own name"
        service = named.group(1)
        assert (_REPO / "systemd" / f"{service}.in").is_file(), (
            f"{unit} names Unit={service} but systemd/{service}.in does not exist"
        )


# ---------------------------------------------------------------------------
# AC6 — the generated units actually append, and name the ticker
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("unit", sorted(_NEW_UNITS))
def test_the_generated_service_appends_and_names_the_ticker(unit: str) -> None:
    """AC6, on the generated `.service`, not on the template.

    ``--append`` is the flag whose absence is invisible: the run succeeds,
    the file parses, and the only consequence is that the file never grows
    past one pull.  ``--ticker`` is checked because these two CLIs take
    ``--ticker`` while funding takes ``--pair`` — getting that backwards
    makes the producer exit non-zero, which at least fails loudly.
    """
    service = _generate_service(
        unit, "ETH/USD", _REPO / "signals" / "eth_usd.jsonl"
    )
    argv = _exec_start_args(service)

    assert "--append" in argv, (
        f"{unit}'s ExecStart has no --append: the producer's default "
        "--lookback-hours window replaces the file, so an hourly run leaves "
        f"one window of history forever. argv={argv!r}"
    )
    assert "--ticker" in argv, (
        f"{unit}'s ExecStart must pass --ticker (these CLIs take --ticker; "
        f"only funding takes --pair). argv={argv!r}"
    )
    assert argv[argv.index("--ticker") + 1] == "ETH/USD", (
        f"{unit}'s ExecStart does not substitute @TICKER@: argv={argv!r}"
    )
    assert argv[argv.index("--output") + 1].endswith("eth_usd.jsonl"), (
        f"{unit}'s ExecStart does not substitute @OUTPUT@: argv={argv!r}"
    )


@pytest.mark.parametrize("unit", sorted(_NEW_UNITS))
def test_the_generated_service_leaves_the_old_file_alone_on_failure(unit: str) -> None:
    """A failed pull must not be mistaken for a fresh file.

    Without this the read has nothing to notice: the JSONL on disk is
    byte-identical to the previous pull's, and the only signal that anything
    went wrong is the staleness bound downstream.
    """
    service = _generate_service(unit, "ETH/USD", _REPO / "signals" / "x.jsonl")
    assert "SuccessExitStatus=0" in service, (
        f"{unit} must declare SuccessExitStatus=0 so a non-zero producer exit "
        "is a failure systemd records, not a success"
    )


@pytest.mark.parametrize("unit", sorted(_NEW_UNITS))
def test_a_failed_generator_still_leaves_a_valid_unit(unit: str) -> None:
    """Both @TICKER@ and @OUTPUT@ must be substituted by the recipe.

    Non-vacuity for the test above: if the recipe forgot a placeholder, the
    generated file would still parse and ``SuccessExitStatus=0`` would still
    be in it, so the failure would only surface as a unit that writes to a
    path literally called ``@OUTPUT@``.
    """
    generated = _generate_service(unit, "ETH/USD", _REPO / "signals" / "x.jsonl")
    argv = _exec_start_args(generated)
    unsubstituted = [token for token in argv if token.startswith("@")]
    assert not unsubstituted, (
        f"{unit} still carries unsubstituted placeholders in its ExecStart: "
        f"{unsubstituted!r} — the unit would write to a path called that"
    )
    recipe = f"{unit[: -len('.service.in')].removeprefix('kraken-trading-bot-')}-timer"
    justfile = (_REPO / "justfile").read_text(encoding="utf-8")
    assert re.search(rf"^{recipe}\b", justfile, re.M), (
        f"justfile has no `{recipe}` recipe, so {unit} cannot be installed"
    )
    # ...and the recipe substitutes BOTH placeholders.
    body = justfile.split(f"\n{recipe} ", 1)[1].split("\n\n", 1)[0]
    assert "@TICKER@" in body or "@PAIR@" in body, f"`just {recipe}` never substitutes the ticker"
    assert "@OUTPUT@" in body, f"`just {recipe}` never substitutes the output path"


# ---------------------------------------------------------------------------
# AC8 — the doc sites say something true
# ---------------------------------------------------------------------------
def test_the_producer_hints_name_a_command_that_runs() -> None:
    """AC8: `_SIGNAL_CHANNELS`' producer strings are copy-pasteable.

    These render into "Produce it with: <here>", so a command that cannot run
    in the dev shell is worse than no command: it is a refusal to discover
    that gnews and vaderSentiment live in the sibling's flake, not here.
    """
    from kraken_trading_bot.rl.data import _SIGNAL_CHANNELS

    for key, producer in _SIGNAL_CHANNELS:
        assert "python ~/Projects/" not in producer, (
            f"{key}'s producer string still advertises `python "
            f"~/Projects/.../cli.py`, which cannot import its own "
            f"dependencies in this dev shell: {producer!r}"
        )
        if key in ("extra_features_file", "social_features_file"):
            assert "nix run ~/Projects/" in producer, (
                f"{key} must name its sibling's own flake: {producer!r}"
            )
            assert "--append" in producer, (
                f"{key}'s producer string omits --append, which is what makes "
                f"the file a log rather than a sliding window: {producer!r}"
            )


def test_the_merge_docstring_no_longer_claims_the_producers_re_append() -> None:
    """AC8: `data.py` says the producers replace, not re-append.

    The old wording was backwards for the two channels that were not yet
    activated, and "re-appends the current hour on every pull" is the reason
    a reader believed a replacing producer was harmless.  It is not: a
    replaced file loses every hour the current pull does not cover.
    """
    from kraken_trading_bot.rl import data

    doc = data.merge_extra_features.__doc__ or ""
    assert "re-appends the current hour on every pull" not in doc, (
        "merge_extra_features' docstring still claims the producers re-append "
        "the current hour; that is what made the truncate look harmless"
    )
    assert "--append" in doc, (
        "the docstring should name the flag that actually produces the "
        "repeated-hour case it de-duplicates"
    )


def test_the_config_comments_document_the_fresh_clone_consequence() -> None:
    """AC8: the shipped config warns about the state a fresh clone is in.

    `signals/` is gitignored (.gitignore:65), so every non-null key is a
    read that raises until a timer has fired.  That is the right behaviour
    (see AC7) and it is still a surprise if the file does not say so.
    """
    text = (_REPO / "configs" / "default.yaml").read_text(encoding="utf-8")
    assert "FRESH CLONE" in text, (
        "configs/default.yaml must warn that a fresh clone has no signal "
        "files and every non-null key therefore raises"
    )
    for key in ("extra_features_file", "funding_features_file", "social_features_file"):
        line = next(
            ln for ln in text.splitlines()
            if ln.startswith(f"{key}:") and not ln.startswith(" " * 4)
        )
        assert "null" not in line.split(":", 1)[1], (
            f"{key} is null in the shipped default, so the channel is off and "
            "the widening this change buys is not active"
        )


# ---------------------------------------------------------------------------
# the AST forwarding guard: every consumer still forwards all three keys
# ---------------------------------------------------------------------------
def test_every_consumer_threads_all_three_signal_file_keys() -> None:
    """Every ``read_ohlc_dataframe`` call site carries all three file keys.

    The generalisation of the guard at
    ``tests/test_rl_signal_config_wiring.py:497-551``: that one proves the
    five keys are threaded for the legs that exist today, this one names the
    three FILE keys specifically, so the leg that forgets one is caught by a
    message that says which file went unforwarded.

    It is a *structural* check on purpose.  ``export`` is proved
    behaviourally in the other file; ``backtest`` and ``paper_trade`` can only
    be reached through a trained model on disk, which would cost a training
    run per assertion for two lines of plumbing.
    """
    from kraken_trading_bot.rl.data import _SIGNAL_CHANNELS

    file_keys = {key for key, _ in _SIGNAL_CHANNELS}
    assert file_keys == {
        "extra_features_file",
        "funding_features_file",
        "social_features_file",
    }, (
        "the three exogenous channels changed shape; update this guard and "
        f"test_rl_data_store.py together. got {sorted(file_keys)}"
    )

    missing: list[str] = []
    for name in ("train", "backtest", "export", "paper_trade"):
        module = __import__(f"kraken_trading_bot.rl.{name}", fromlist=["*"])
        source = Path(inspect.getsourcefile(module) or "")
        tree = ast.parse(source.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "read_ohlc_dataframe"
        ]
        if not calls:
            missing.append(f"{name}: no read_ohlc_dataframe call found")
            continue
        keywords = {kw.arg for kw in calls[0].keywords if kw.arg}
        for key in sorted(file_keys):
            if key not in keywords:
                missing.append(f"{name}: does not forward {key}")

    assert not missing, "signal file wiring gaps: " + "; ".join(missing)
