"""Exercise real tooltip event handlers with a small DOM and deterministic clock."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


_JS = Path(__file__).resolve().parent.parent / "static" / "js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")

_BROWSER = r"""
import assert from 'node:assert/strict';

class EventTargetShim {
  listeners = new Map();
  addEventListener(type, callback) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(callback);
  }
  removeEventListener(type, callback) { this.listeners.get(type)?.delete(callback); }
  dispatchEvent(event) {
    event.target ??= this;
    event.preventDefault ??= () => { event.defaultPrevented = true; };
    event.stopPropagation ??= () => {};
    event.stopImmediatePropagation ??= () => { event.immediateStopped = true; };
    for (const callback of [...(this.listeners.get(event.type) || [])]) {
      callback(event);
      if (event.immediateStopped) break;
    }
    return !event.defaultPrevented;
  }
  listenerCount(type) { return this.listeners.get(type)?.size || 0; }
}

class Element extends EventTargetShim {
  constructor(tagName) {
    super();
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.attributes = new Map();
  }
  appendChild(child) { child.parentElement = this; this.children.push(child); return child; }
  remove() {
    this.parentElement.children = this.parentElement.children.filter(child => child !== this);
    this.parentElement = null;
  }
  contains(target) {
    for (let node = target; node; node = node.parentElement) if (node === this) return true;
    return false;
  }
  closest(selector) {
    if (selector === 'button[data-help-hint]') {
      for (let node = this; node; node = node.parentElement) {
        if (node.tagName === 'BUTTON' && node.dataset.helpHint) return node;
      }
    }
    return null;
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  getBoundingClientRect() { return { left: 100, top: 100, bottom: 124, width: 200, height: 40 }; }
  getClientRects() { return [this.getBoundingClientRect()]; }
  get isConnected() { return document.body.contains(this); }
}

globalThis.document = new EventTargetShim();
document.body = new Element('body');
document.documentElement = new Element('html');
document.createElement = tag => new Element(tag);
document.activeElement = document.body;
globalThis.window = new EventTargetShim();
window.innerWidth = 1280;
window.innerHeight = 720;
globalThis.getComputedStyle = () => ({ zoom: '1', zIndex: '100' });
globalThis.MutationObserver = class { observe() {} disconnect() {} };
globalThis.CustomEvent = class {
  constructor(type, options = {}) { this.type = type; this.detail = options.detail; }
};

let now = 0;
let nextTimer = 0;
const timers = new Map();
globalThis.setTimeout = (callback, delay) => {
  const id = ++nextTimer;
  timers.set(id, { callback, due: now + delay });
  return id;
};
globalThis.clearTimeout = id => timers.delete(id);
function advance(milliseconds) {
  const end = now + milliseconds;
  while (true) {
    const entry = [...timers].filter(([, timer]) => timer.due <= end)
      .sort((a, b) => a[1].due - b[1].due)[0];
    if (!entry) break;
    const [id, timer] = entry;
    now = timer.due;
    timers.delete(id);
    timer.callback();
  }
  now = end;
}

const modal = document.body.appendChild(new Element('section'));
const anchor = modal.appendChild(new Element('button'));
anchor.dataset.helpHint = 'A useful explanation';
anchor.setAttribute('aria-expanded', 'false');
const icon = anchor.appendChild(new Element('span'));
const outside = document.body.appendChild(new Element('button'));
function emit(on, type, target, options = {}) {
  const event = { type, target, pointerType: 'mouse', relatedTarget: null, ...options };
  on.dispatchEvent(event);
  return event;
}
function popup() { return document.body.children.find(node => node.className === 'settings-help-tooltip'); }
function focusAnchor() {
  document.activeElement = anchor;
  emit(modal, 'focusin', anchor);
}
function clickAnchor(detail = 1) { emit(modal, 'click', icon, { detail }); }
function leaveAnchor(pointerType = 'mouse') { emit(modal, 'pointerout', anchor, { relatedTarget: outside, pointerType }); }
function assertOpen() {
  assert.ok(popup(), 'tooltip should be visible');
  assert.equal(anchor.getAttribute('aria-expanded'), 'true');
  assert.equal(anchor.getAttribute('aria-describedby'), popup().id);
  assert.equal(menus._openMenuCount(), 1);
}
function assertClosed() {
  assert.equal(popup(), undefined, 'tooltip should be dismissed');
  assert.equal(anchor.getAttribute('aria-expanded'), 'false');
  assert.equal(anchor.getAttribute('aria-describedby'), null);
  assert.equal(menus._openMenuCount(), 0, 'Escape registration must be released');
  assert.equal(document.listenerCount('pointerdown'), 0);
  assert.equal(window.listenerCount('keydown'), 0);
  assert.equal(timers.size, 0, 'leave timer must be released');
}
"""


def _run(body, mode="click"):
    source = _BROWSER + f"""
    globalThis.fetch = async () => ({{ ok: true, json: async () => ({{ value: {json.dumps(mode)} }}) }});
    const behavior = await import({json.dumps((_JS / 'settings/behavior.js').as_uri())});
    const hints = await import({json.dumps((_JS / 'settings/helpHints.js').as_uri())});
    const menus = await import({json.dumps((_JS / 'escMenuStack.js').as_uri())});
    await behavior.loadTooltipTrigger();
    hints.bindSettingsHelp(modal);
    """ + body
    result = subprocess.run(
        ["node", "--input-type=module"],
        input=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=_JS.parent.parent,
        timeout=15,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_click_mode_waits_for_activation():
    _run("""
        emit(modal, 'pointerover', icon);
        focusAnchor();
        assertClosed();
        clickAnchor();
        assertOpen();
    """)


@pytest.mark.parametrize("mode", ["hover", "click"])
def test_pointer_clicked_tooltip_closes_after_leave_even_when_anchor_is_focused(mode):
    _run("""
        focusAnchor();
        clickAnchor();
        assertOpen();
        leaveAnchor();
        advance(149);
        assertOpen();
        assert.equal(document.activeElement, anchor);
        advance(1);
        assertClosed();
    """, mode)


@pytest.mark.parametrize("mode", ["hover", "click"])
def test_entering_popup_cancels_leave_then_leaving_popup_closes(mode):
    _run("""
        focusAnchor();
        clickAnchor();
        const bubble = popup();
        emit(modal, 'pointerout', anchor, { relatedTarget: bubble });
        advance(75);
        emit(bubble, 'pointerenter', bubble, { relatedTarget: anchor });
        advance(1000);
        assertOpen();
        emit(bubble, 'pointerleave', bubble, { relatedTarget: outside });
        advance(150);
        assertClosed();
    """, mode)


@pytest.mark.parametrize("mode", ["hover", "click"])
def test_returning_to_anchor_cancels_pending_leave(mode):
    _run("""
        focusAnchor();
        clickAnchor();
        leaveAnchor();
        advance(75);
        emit(modal, 'pointerover', icon, { relatedTarget: outside });
        advance(1000);
        assertOpen();
        leaveAnchor();
        advance(150);
        assertClosed();
    """, mode)


@pytest.mark.parametrize("dismissal", ["click", "outside", "escape"])
def test_explicit_dismissal_closes_and_releases_handlers(dismissal):
    _run(f"const dismissal = {json.dumps(dismissal)};" + """
        clickAnchor();
        assertOpen();
        // An inside press is not an outside dismissal.
        emit(document, 'pointerdown', popup());
        assertOpen();
        if (dismissal === 'click') clickAnchor();
        else if (dismissal === 'outside') emit(document, 'pointerdown', outside);
        else {
          const event = emit(window, 'keydown', anchor, { key: 'Escape' });
          assert.equal(event.defaultPrevented, true);
          assert.equal(event.immediateStopped, true);
        }
        assertClosed();
    """)


@pytest.mark.parametrize("dismissal", ["blur", "escape"])
def test_keyboard_activation_survives_pointer_leave_until_blur_or_escape(dismissal):
    _run(f"const dismissal = {json.dumps(dismissal)};" + """
        focusAnchor();
        clickAnchor(0);
        leaveAnchor();
        advance(1000);
        assertOpen();
        if (dismissal === 'blur') {
          document.activeElement = outside;
          emit(modal, 'focusout', anchor, { relatedTarget: outside });
        } else emit(window, 'keydown', anchor, { key: 'Escape' });
        assertClosed();
    """)


def test_hover_mode_keyboard_focus_stays_open_until_blur():
    _run("""
        focusAnchor();
        assertOpen();
        leaveAnchor();
        advance(1000);
        assertOpen();
        document.activeElement = outside;
        emit(modal, 'focusout', anchor, { relatedTarget: outside });
        assertClosed();
    """, "hover")


@pytest.mark.parametrize("surface", ["anchor", "popup"])
def test_touch_pointer_exit_does_not_dismiss_tooltip(surface):
    _run(f"const surface = {json.dumps(surface)};" + """
        focusAnchor();
        clickAnchor();
        if (surface === 'anchor') leaveAnchor('touch');
        else emit(popup(), 'pointerleave', popup(), { pointerType: 'touch', relatedTarget: outside });
        advance(1000);
        assertOpen();
        emit(document, 'pointerdown', outside, { pointerType: 'touch' });
        assertClosed();
    """)
