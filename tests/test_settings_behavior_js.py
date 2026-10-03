"""Run the real per-user tooltip preference module with controlled requests."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parent.parent
_MODULE = _REPO / "static" / "js" / "settings" / "behavior.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")

_BROWSER = r"""
import assert from 'node:assert/strict';
const calls = [];
const responses = [];
const events = [];
globalThis.fetch = (url, options = {}) => {
  calls.push({ url, options });
  assert.ok(responses.length, 'unexpected request');
  return responses.shift()();
};
globalThis.document = { dispatchEvent(event) { events.push(event); } };
globalThis.CustomEvent = class {
  constructor(type, options = {}) { this.type = type; this.detail = options.detail; }
};
const queueJSON = (value, ok = true) => responses.push(async () => ({
  ok, status: ok ? 200 : 503, json: async () => value,
}));
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};
"""


def _run(body):
    source = (
        _BROWSER
        + f"\nconst behavior = await import({json.dumps(_MODULE.as_uri())});\n"
        + body
    )
    result = subprocess.run(
        ["node", "--input-type=module"],
        input=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=_REPO,
        timeout=15,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


@pytest.mark.parametrize("payload", [{}, {"value": None}, {"value": "invalid"}, {"value": {}}, {"value": []}])
def test_missing_or_invalid_saved_preference_defaults_to_hover(payload):
    _run(
        f"queueJSON({json.dumps(payload)});\n"
        + """
        assert.equal(behavior.getTooltipTrigger(), 'hover');
        await behavior.loadTooltipTrigger();
        assert.equal(behavior.getTooltipTrigger(), 'hover');
        assert.equal(calls.length, 1);
        """
    )


def test_concurrent_and_later_reads_share_one_owner_scoped_request():
    _run("""
        const response = deferred();
        responses.push(() => response.promise);
        const first = behavior.loadTooltipTrigger();
        const second = behavior.loadTooltipTrigger();
        response.resolve({ ok: true, json: async () => ({ value: 'click' }) });
        await Promise.all([first, second]);
        await behavior.loadTooltipTrigger();
        assert.equal(behavior.getTooltipTrigger(), 'click');
        assert.equal(calls.length, 1);
        assert.equal(calls[0].url, '/api/prefs/tooltip-trigger');
        assert.equal(calls[0].options.credentials, 'same-origin');
    """)


@pytest.mark.parametrize("failure", ["http", "network", "json"])
def test_failed_load_can_retry(failure):
    _run(
        f"const failure = {json.dumps(failure)};\n"
        + """
        if (failure === 'http') queueJSON({ value: 'click' }, false);
        else responses.push(async () => {
          if (failure === 'network') throw new Error('offline');
          return { ok: true, json: async () => { throw new Error('bad JSON'); } };
        });
        await assert.rejects(behavior.loadTooltipTrigger());
        assert.equal(behavior.getTooltipTrigger(), 'hover');
        queueJSON({ value: 'click' });
        await behavior.loadTooltipTrigger();
        assert.equal(behavior.getTooltipTrigger(), 'click');
        assert.equal(calls.length, 2);
        """
    )


def test_save_applies_and_notifies_only_after_success():
    _run("""
        const response = deferred();
        responses.push(() => response.promise);
        const saving = behavior.saveTooltipTrigger('click');
        assert.equal(behavior.getTooltipTrigger(), 'hover');
        assert.equal(events.length, 0);
        response.resolve({ ok: true, json: async () => ({ value: 'click' }) });
        await saving;
        assert.equal(behavior.getTooltipTrigger(), 'click');
        assert.equal(calls.length, 1);
        assert.equal(calls[0].url, '/api/prefs/tooltip-trigger');
        assert.equal(calls[0].options.method, 'PUT');
        assert.equal(calls[0].options.credentials, 'same-origin');
        assert.equal(calls[0].options.headers['Content-Type'], 'application/json');
        assert.deepEqual(JSON.parse(calls[0].options.body), { value: 'click' });
        assert.deepEqual(events.map(event => event.type), ['odysseus:tooltip-trigger-change']);
    """)


@pytest.mark.parametrize("failure", ["http", "network"])
def test_failed_save_keeps_the_last_confirmed_choice(failure):
    _run(
        f"const failure = {json.dumps(failure)};\n"
        + """
        queueJSON({ value: 'click' });
        await behavior.loadTooltipTrigger();
        const priorEvents = events.length;
        if (failure === 'http') queueJSON({ value: 'hover' }, false);
        else responses.push(async () => { throw new Error('offline'); });
        await assert.rejects(behavior.saveTooltipTrigger('hover'));
        assert.equal(behavior.getTooltipTrigger(), 'click');
        assert.equal(events.length, priorEvents);
        queueJSON({ value: 'hover' });
        await behavior.saveTooltipTrigger('hover');
        assert.equal(behavior.getTooltipTrigger(), 'hover');
        """
    )


@pytest.mark.parametrize("value", [None, "", "invalid", {}])
def test_invalid_choice_is_rejected_before_sending_request(value):
    _run(
        f"const value = {json.dumps(value)};\n"
        + """
        await assert.rejects(async () => behavior.saveTooltipTrigger(value));
        assert.equal(calls.length, 0);
        assert.equal(behavior.getTooltipTrigger(), 'hover');
        assert.equal(events.length, 0);
        """
    )


def test_stale_load_cannot_overwrite_a_newly_saved_choice():
    _run("""
        const response = deferred();
        responses.push(() => response.promise);
        const loading = behavior.loadTooltipTrigger();
        queueJSON({ value: 'click' });
        await behavior.saveTooltipTrigger('click');
        const priorEvents = events.length;
        response.resolve({ ok: true, json: async () => ({ value: 'hover' }) });
        await loading;
        await behavior.loadTooltipTrigger();
        assert.equal(behavior.getTooltipTrigger(), 'click');
        assert.equal(events.length, priorEvents);
        assert.equal(calls.length, 2);
    """)
