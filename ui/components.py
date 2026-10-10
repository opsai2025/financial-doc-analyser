"""Reusable UI building blocks implementing the design rules (DECISIONS §9)."""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import streamlit as st

from services.auth_routes import PING_REPLY

APP_TITLE = "سامانه تحلیل هوشمند گزارش‌های حسابرسی"
APP_SUBTITLE = "تحلیل گزارش حسابرس مستقل و بازرس قانونی با هوش مصنوعی"

_STYLES = Path(__file__).with_name("styles.css")

# Icons are served from static/ (inline SVG is stripped by st.html's sanitizer).
# Relative URLs work because every page lives at a single-segment path.
_LOGO = '<img src="app/static/icons/{name}.svg" width="{size}" height="{size}" alt="">'

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$:])")


def md_escape(text: object) -> str:
    """Escape user/LLM text before putting it into st.markdown."""
    return _MD_SPECIAL.sub(r"\\\1", str(text))


def _key(prefix: str, seed: str) -> str:
    return f"{prefix}_{hashlib.md5(seed.encode('utf-8')).hexdigest()[:10]}"


def inject_theme() -> None:
    st.html(_STYLES)


def app_header(title: str = APP_TITLE, subtitle: str = APP_SUBTITLE) -> None:
    st.html(
        f"""
        <div class="fda-header">
          <div class="fda-header-icon">{_LOGO.format(name="logo", size=32)}</div>
          <div>
            <h1>{html.escape(title)}</h1>
            <p>{html.escape(subtitle)}</p>
          </div>
        </div>
        """
    )


def sidebar_brand() -> None:
    st.html(
        f"""
        <div class="fda-brand">
          <div class="fda-brand-icon">{_LOGO.format(name="logo-dark", size=24)}</div>
          <div class="fda-brand-title">{html.escape(APP_TITLE)}</div>
        </div>
        """
    )


def page_title(title: str, icon: str, caption: str | None = None) -> None:
    """Module title under the app header (icon = Material Symbols name)."""
    st.header(f":material/{icon}: {title}", anchor=False)
    if caption:
        st.caption(caption)


def help_box(
    title: str,
    items: Sequence[str] | str,
    *,
    variant: str = "blue",
    icon: str = "info",
    expanded: bool | None = None,
    key: str | None = None,
) -> None:
    """Guide/notice box. Long guides use bullets; set `expanded` to render it collapsible."""
    body = items if isinstance(items, str) else "\n".join(f"- {item}" for item in items)
    container_key = _key(f"help{variant}", key or title)
    with st.container(key=container_key):
        if expanded is None:
            st.markdown(f"**:material/{icon}: {title}**\n\n{body}")
        else:
            with st.expander(f":material/{icon}: {title}", expanded=expanded):
                st.markdown(body)


@dataclass(frozen=True)
class Card:
    label: str
    value: str
    icon: str
    tone: str = "neutral"  # neutral | success | info | warning | danger
    help: str | None = None


def cards_row(cards: Iterable[Card], *, key: str) -> None:
    cards = list(cards)
    columns = st.columns(len(cards))
    for idx, (col, card) in enumerate(zip(columns, cards, strict=True)):
        with col, st.container(key=f"card_{card.tone}_{key}_{idx}"):
            st.metric(card.label, card.value, icon=f":material/{card.icon}:", help=card.help)


def _js_string(value: str) -> str:
    return json.dumps(value).replace("</", "<\\/")  # a JS string literal that cannot close the script tag


def redirect(url: str, fallback: str) -> None:
    """Navigate the browser to a cookie route (`auth/...`), or to `fallback` when the host does not serve
    those routes (app not launched through `app.py`). URLs must be app-generated, never user input."""
    st.html(
        f"""<script>{{
        const go = (u) => window.location.replace(u);
        fetch("auth/ping", {{cache: "no-store"}})
          .then((r) => r.text())
          .then((t) => go(t === {_js_string(PING_REPLY)} ? {_js_string(url)} : {_js_string(fallback)}))
          .catch(() => go({_js_string(fallback)}));
        }}</script>""",
        unsafe_allow_javascript=True,
    )


def show_errors(exc: Exception) -> None:
    """Show a service validation error (one message per line) as a bullet list."""
    st.error("\n".join(f"- {line}" for line in str(exc).splitlines()), icon=":material/error:")


def confirm_button(label: str, *, key: str, prompt: str, icon: str = ":material/delete:") -> bool:
    """A danger button that asks for confirmation in a popover. Returns True once confirmed."""
    with st.popover(label, icon=icon):
        st.markdown(prompt)
        return st.button("بله، انجام شود", key=f"danger_{key}", icon=":material/check:", width="stretch")


def file_label(name: str) -> str:
    """A file name as isolated text, so "1398.pdf" isn't shown as "pdf.1398" in RTL widgets (D-12.16)."""
    return f"⁨{name}⁩"


def usage_bar(label: str, value_text: str, fraction: float) -> None:
    """Quota usage bar (D-12.14): green, orange from 70 %, red when full."""
    fraction = max(0.0, min(1.0, fraction))
    tone = "full" if fraction >= 1 else "high" if fraction >= 0.7 else "ok"
    st.html(
        f"""
        <div class="fda-usage fda-usage-{tone}">
          <div class="fda-usage-text"><span>{html.escape(label)}</span><span>{html.escape(value_text)}</span></div>
          <div class="fda-usage-track"><div class="fda-usage-fill" style="width:{fraction * 100:.1f}%"></div></div>
        </div>
        """
    )
