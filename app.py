#!/usr/bin/env python3
"""
Arb Scanner — Streamlit web app
Wraps arb_scanner_v4 core logic in a shareable browser UI.
"""

import sys
import os
import arb_scanner_v4 as _core

import streamlit as st
from datetime import datetime

from arb_scanner_v4 import (
    to_payout, fmt_odds, time_until,
    scan_all_sports, analyze_multileg_entry,
    PRIORITY_SPORTS, PLATFORM_MULTIPLIERS, PROP_MARKET_MAP, SPORT_MAP, EST,
)

# ── page config ────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Arb Scanner",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── sidebar ────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("⚙️ Settings")
    min_margin = st.slider("Min margin %", 1.0, 5.0, 2.0, 0.5,
                           help="Higher = fewer but more reliable arbs")
    _sport_labels = {
        "basketball_nba":            "NBA",
        "icehockey_nhl":             "NHL",
        "baseball_mlb":              "MLB",
        "soccer_epl":                "EPL",
        "soccer_usa_mls":            "MLS",
        "soccer_uefa_champs_league": "UEFA Champions League",
        "mma_mixed_martial_arts":    "MMA",
        "basketball_ncaab":          "NCAAB",
        "americanfootball_nfl":      "NFL",
    }
    st.caption("Sports scanned:")
    for s in PRIORITY_SPORTS:
        st.caption(f"  · {_sport_labels.get(s, s.replace('_', ' ').title())}")

# ── header ─────────────────────────────────────────────────────────────────

st.title("📊 Arb Scanner")
st.caption("Sportsbook arbs + PrizePicks / Underdog multi-leg prop arbs")
st.divider()

tab_game, tab_prop = st.tabs(["🏟️  Game Arbs", "🎯  Prop Arbs"])

# ══════════════════════════════════════════════════════════════════════════
# GAME ARBS TAB
# ══════════════════════════════════════════════════════════════════════════

with tab_game:

    col_hdr, col_btn = st.columns([4, 1])
    with col_hdr:
        if "last_scan_time" in st.session_state:
            st.caption(f"Last scanned: {st.session_state.last_scan_time}")
    with col_btn:
        scan_btn = st.button("🔍  Scan Now", type="primary", use_container_width=True)

    # Run scan on first load or button press
    if scan_btn or "game_arbs" not in st.session_state:
        with st.spinner("Scanning all sports in parallel…"):
            orig = _core.MIN_MARGIN
            _core.MIN_MARGIN = min_margin
            _core._events_cache.clear()
            all_arbs, total_events = scan_all_sports(PRIORITY_SPORTS)
            _core.MIN_MARGIN = orig

            all_arbs.sort(key=lambda a: a["margin"], reverse=True)
            st.session_state.game_arbs = all_arbs
            st.session_state.total_events = total_events
            st.session_state.last_scan_time = datetime.now(EST).strftime("%I:%M:%S %p ET")

    arbs = st.session_state.get("game_arbs", [])
    total_events = st.session_state.get("total_events", 0)

    # Metrics row
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Events scanned", total_events)
    m2.metric("Arbs found", len(arbs))
    m3.metric("Best margin", f"{arbs[0]['margin']:.2f}%" if arbs else "—")
    m4.metric("Min margin set", f"{min_margin:.1f}%")

    st.divider()

    mk_labels = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}

    if not arbs:
        st.info("No arbs found above the threshold. Lower the min margin or scan again in 15 minutes.")
    else:
        for i, arb in enumerate(arbs, 1):
            ev = arb["ev"]
            margin = arb["margin"]
            mk = mk_labels.get(arb["mk"], arb["mk"])
            countdown = time_until(ev["commence_time"])
            sport = ev["sport_key"].replace("_", " ").title()

            if margin >= 3.5:
                badge = f"🟢 {margin:.2f}%"
            elif margin >= 2.5:
                badge = f"🟡 {margin:.2f}%"
            else:
                badge = f"🔴 {margin:.2f}%"

            label = f"#{i} · {ev['home_team']} vs {ev['away_team']}  ·  {mk}  ·  {badge}  ·  starts in {countdown}"

            with st.expander(label):
                st.caption(f"{sport}")

                stake = st.number_input(
                    "Stake ($)",
                    min_value=1.0,
                    value=100.0,
                    step=10.0,
                    key=f"game_stake_{i}",
                )

                ocs = arb["ocs"]
                oc_sum = arb["sum"]

                rows = []
                payouts = []
                for name, o in ocs.items():
                    st_amt = stake * (o["implied"] / oc_sum)
                    pay = to_payout(st_amt, o["price"])
                    payouts.append(pay)
                    display = name.split("|")[0] if "|" in name else name
                    rows.append({
                        "Sportsbook": o["book"],
                        "Bet on":     display,
                        "Odds":       fmt_odds(o["price"]),
                        "Stake":      f"${st_amt:.2f}",
                        "Payout":     f"${pay:.2f}",
                    })

                st.table(rows)

                guaranteed = min(payouts) - stake
                gcol, _ = st.columns([1, 3])
                color = "normal" if guaranteed >= 0 else "inverse"
                gcol.metric("Guaranteed profit", f"${guaranteed:.2f}", delta_color=color)


# ══════════════════════════════════════════════════════════════════════════
# PROP ARBS TAB
# ══════════════════════════════════════════════════════════════════════════

with tab_prop:
    st.subheader("PrizePicks / Underdog Multi-Leg Prop Arb")

    p1, p2, p3 = st.columns(3)
    with p1:
        platform_key = st.selectbox("Platform", ["PrizePicks", "Underdog"])
    with p2:
        multipliers = PLATFORM_MULTIPLIERS[platform_key]
        num_legs = st.selectbox("Legs", list(multipliers.keys()))
        multiplier = multipliers[num_legs]
    with p3:
        total_stake = st.number_input("Entry stake ($)", min_value=1.0, value=25.0, step=5.0)

    st.caption(
        f"{num_legs}-leg entry pays **{multiplier}x** → **${total_stake * multiplier:.2f}** if all legs hit"
    )
    st.divider()
    st.markdown("**Enter your legs:**")

    stat_options  = list(PROP_MARKET_MAP.keys())
    sport_options = list(SPORT_MAP.keys())

    legs_input = []
    for leg_num in range(1, num_legs + 1):
        st.markdown(f"**Leg {leg_num}**")
        c1, c2, c3, c4, c5, c6 = st.columns([2, 1.5, 1.5, 1, 1.2, 1])
        player    = c1.text_input("Player",  key=f"p_{leg_num}", placeholder="LeBron James")
        team      = c2.text_input("Team",    key=f"t_{leg_num}", placeholder="Lakers")
        stat      = c3.selectbox("Stat",     stat_options,  key=f"s_{leg_num}")
        line      = c4.number_input("Line",  key=f"l_{leg_num}", value=25.5, step=0.5, format="%.1f")
        direction = c5.selectbox("Pick",     ["Over", "Under"], key=f"d_{leg_num}")
        sport_raw = c6.selectbox("Sport",    sport_options, key=f"sp_{leg_num}")

        legs_input.append({
            "player":    player,
            "team":      team or None,
            "stat":      stat,
            "line":      line,
            "direction": direction,
            "sport":     SPORT_MAP[sport_raw],
        })

    st.divider()
    analyze_btn = st.button("Analyze Entry", type="primary")

    if analyze_btn:
        missing = [i + 1 for i, l in enumerate(legs_input) if not l["player"].strip()]
        if missing:
            st.error(f"Missing player name for leg(s): {missing}")
        else:
            with st.spinner("Fetching sportsbook odds for all legs…"):
                orig = _core.MIN_MARGIN
                _core.MIN_MARGIN = min_margin
                leg_results, mult, dfs_payout = analyze_multileg_entry(
                    legs_input, platform_key, num_legs, total_stake
                )
                _core.MIN_MARGIN = orig

            hedged   = [l for l in leg_results if l["hedgeable"]]
            unhedged = [l for l in leg_results if not l["hedgeable"]]

            # Summary metrics
            s1, s2, s3 = st.columns(3)
            s1.metric("DFS payout (all hit)", f"${dfs_payout:.2f}")
            s2.metric("Legs to hedge", f"{len(hedged)} / {len(leg_results)}")

            if hedged:
                hedge_per    = total_stake / len(hedged)
                total_hedge  = hedge_per * len(hedged)
                hedge_pays   = [to_payout(hedge_per, l["hedge_odds"]) for l in hedged]
                best_case    = dfs_payout - total_stake - total_hedge
                worst_case   = sum(hedge_pays) - total_stake - total_hedge
                s3.metric("Best case net", f"${best_case:+.2f}")

                st.divider()

                # DFS slip
                st.markdown("**DFS Entry**")
                dfs_rows = []
                for i, l in enumerate(leg_results, 1):
                    dfs_rows.append({
                        "Leg":    i,
                        "Player": l["player"],
                        "Stat":   l["stat"],
                        "Pick":   l["direction"],
                        "Line":   l["line"],
                        "Hedge?": "✓ hedge" if l["hedgeable"] else "— no hedge",
                    })
                st.table(dfs_rows)

                # Sportsbook hedge slip
                st.markdown(f"**Sportsbook Hedges** — ${hedge_per:.2f} each")
                hedge_rows = []
                for l, pay in zip(hedged, hedge_pays):
                    hedge_rows.append({
                        "Sportsbook": l["hedge_book"],
                        "Player":     l["player"],
                        "Bet":        f"{l['hedge_direction']} {l['line']}",
                        "Odds":       fmt_odds(l["hedge_odds"]),
                        "Stake":      f"${hedge_per:.2f}",
                        "Payout":     f"${pay:.2f}",
                    })
                st.table(hedge_rows)

                # Outcome scenarios
                st.divider()
                st.markdown("**Outcome Scenarios**")
                o1, o2 = st.columns(2)
                o1.metric(
                    "All legs HIT — best case",
                    f"${best_case:+.2f}",
                    help=f"DFS ${dfs_payout:.2f} − entry ${total_stake:.2f} − hedges lost ${total_hedge:.2f}",
                )
                o2.metric(
                    "All legs MISS, hedges win — floor",
                    f"${worst_case:+.2f}",
                    help=f"Hedge payouts ${sum(hedge_pays):.2f} − entry ${total_stake:.2f} − hedge stakes ${total_hedge:.2f}",
                )

                if unhedged:
                    st.warning(
                        f"{len(unhedged)} unhedged leg(s) — if any of these miss, the full DFS entry loses with no sportsbook offset."
                    )
            else:
                s3.metric("Best case net", "—")
                st.warning(
                    "No legs meet the minimum margin threshold. "
                    "Lower the min margin in the sidebar or choose different legs."
                )
