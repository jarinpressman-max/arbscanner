#!/usr/bin/env python3
"""
Arb Scanner — Streamlit web app
Wraps arb_scanner_v4 core logic in a shareable browser UI.
"""

import sys
import os
import time
import arb_scanner_v4 as _core

import streamlit as st
from streamlit_autorefresh import st_autorefresh
from datetime import datetime

from arb_scanner_v4 import (
    to_payout, fmt_odds, time_until,
    scan_all_sports, analyze_multileg_entry, find_ev_bets,
    get_prizepicks_projections, find_prizepicks_ev,
    PRIORITY_SPORTS, PLATFORM_MULTIPLIERS, PROP_MARKET_MAP, SPORT_MAP,
    PRIZEPICKS_LEAGUE_SPORT, EST,
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
    min_margin = st.slider("Min margin %", 0.5, 5.0, 1.0, 0.5,
                           help="Higher = fewer but more reliable arbs")

    st.divider()

    # Kelly Criterion bankroll
    st.markdown("**Kelly Criterion**")
    bankroll = st.number_input(
        "Bankroll ($)", min_value=0.0, value=0.0, step=100.0, format="%.2f",
        help="Enter your bankroll to see Kelly-recommended stake sizes. Leave at 0 to skip."
    )

    st.divider()

    # Auto-refresh
    st.markdown("**Auto-Refresh**")
    auto_refresh = st.toggle("Enable auto-refresh", value=False)
    if auto_refresh:
        refresh_mins = st.selectbox("Refresh every", [5, 10, 15, 30], index=2)
        st_autorefresh(interval=refresh_mins * 60 * 1000, key="autorefresh")
        st.caption(f"Refreshing every {refresh_mins} min")

    st.divider()

    # Bookmaker exclude filter
    st.markdown("**Exclude Bookmakers**")
    all_books = st.session_state.get("all_books", [])
    if all_books:
        excluded_books = st.multiselect(
            "Hide arbs containing these books",
            options=sorted(all_books),
            default=[],
        )
    else:
        excluded_books = []
        st.caption("Run a scan to see available bookmakers")

    st.divider()

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

tab_game, tab_prop, tab_ev, tab_pp = st.tabs(["🏟️  Game Arbs", "🎯  Prop Arbs", "📈  EV+ Bets", "🏆  PrizePicks"])

# ══════════════════════════════════════════════════════════════════════════
# SHARED SCAN LOGIC  (runs once, results shared across tabs)
# ══════════════════════════════════════════════════════════════════════════

def _cache_seconds_remaining():
    """Seconds until the oldest cached sport entry expires. 0 = stale/empty."""
    if not _core._api_cache:
        return 0
    oldest_ts = min(ts for ts, _ in _core._api_cache.values())
    return max(0, _core._CACHE_TTL - (time.time() - oldest_ts))


def _collect_all_events():
    """Pull every cached event list into a flat list (for EV+ scan)."""
    events = []
    seen_ids = set()
    for _ts, data in _core._api_cache.values():
        for ev in data:
            eid = ev.get("id")
            if eid not in seen_ids:
                seen_ids.add(eid)
                events.append(ev)
    return events


# ══════════════════════════════════════════════════════════════════════════
# GAME ARBS TAB
# ══════════════════════════════════════════════════════════════════════════

with tab_game:

    col_hdr, col_btn = st.columns([4, 1])
    with col_hdr:
        if "last_scan_time" in st.session_state:
            secs_left = _cache_seconds_remaining()
            if secs_left > 0:
                mins, secs = divmod(int(secs_left), 60)
                st.caption(
                    f"Last scanned: {st.session_state.last_scan_time}  ·  "
                    f"⚡ Cached — next free scan in {mins}m {secs:02d}s"
                )
            else:
                st.caption(f"Last scanned: {st.session_state.last_scan_time}  ·  ✅ Cache expired — ready for fresh scan")
    with col_btn:
        scan_btn = st.button("🔍  Scan Now", type="primary", use_container_width=True)

    # Run scan on first load or button press
    if scan_btn or "game_arbs" not in st.session_state:
        secs_left = _cache_seconds_remaining()
        if secs_left > 0 and "game_arbs" in st.session_state:
            # Still within cache window — reuse existing results, no API call
            pass
        else:
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

                # Collect all books seen across all cached events
                books_seen = set()
                for _ts, data in _core._api_cache.values():
                    for ev in data:
                        for bm in ev.get("bookmakers", []):
                            books_seen.add(bm["title"])
                st.session_state.all_books = list(books_seen)

                # Run EV+ scan on cached events
                all_events = _collect_all_events()
                st.session_state.ev_bets = find_ev_bets(all_events, min_ev=2.0)

    arbs = st.session_state.get("game_arbs", [])
    total_events = st.session_state.get("total_events", 0)

    # Apply bookmaker exclude filter
    if excluded_books:
        arbs = [
            a for a in arbs
            if not any(o["book"] in excluded_books for o in a["ocs"].values())
        ]

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

                # Kelly Criterion stake suggestion
                if bankroll > 0:
                    kelly_stake = bankroll * (margin / 100)
                    st.caption(f"💡 Kelly stake: **${kelly_stake:.2f}** ({margin:.2f}% of ${bankroll:,.0f} bankroll)")
                    default_stake = f"{kelly_stake:.2f}"
                else:
                    default_stake = "100"

                _stake_str = st.text_input(
                    "Stake ($)", value=default_stake, key=f"game_stake_{i}"
                )
                try:
                    stake = float(_stake_str)
                    if stake <= 0:
                        raise ValueError
                except ValueError:
                    st.error("Enter a valid stake amount (e.g. 100 or 500)")
                    stake = 100.0

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
        total_stake = st.number_input("Entry stake ($)", min_value=1.0, value=25.0, step=1.0, format="%.2f")

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


# ══════════════════════════════════════════════════════════════════════════
# EV+ BETS TAB
# ══════════════════════════════════════════════════════════════════════════

with tab_ev:
    st.subheader("📈 EV+ Bets")
    st.caption(
        "Bets where one book's implied probability is **lower than the market consensus** — "
        "meaning you're getting better-than-fair odds. Requires ≥3 books pricing the same outcome."
    )
    st.divider()

    ev_bets = st.session_state.get("ev_bets", [])

    if not ev_bets:
        if "game_arbs" not in st.session_state:
            st.info("Run a scan from the Game Arbs tab first to populate EV+ bets.")
        else:
            st.info("No EV+ bets found above 2% edge. Try lowering the min margin or scan again.")
    else:
        mk_labels = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}

        e1, e2, e3 = st.columns(3)
        e1.metric("EV+ bets found", len(ev_bets))
        e2.metric("Best edge", f"{ev_bets[0]['ev_pct']:.2f}%")
        e3.metric("Avg books compared", f"{sum(b['num_books'] for b in ev_bets) / len(ev_bets):.1f}")

        st.divider()

        for i, bet in enumerate(ev_bets, 1):
            ev = bet["ev"]
            ev_pct = bet["ev_pct"]
            mk = mk_labels.get(bet["mk"], bet["mk"])
            countdown = time_until(ev["commence_time"])
            sport = ev["sport_key"].replace("_", " ").title()

            if ev_pct >= 5.0:
                badge = f"🟢 +{ev_pct:.2f}%"
            elif ev_pct >= 3.0:
                badge = f"🟡 +{ev_pct:.2f}%"
            else:
                badge = f"🔴 +{ev_pct:.2f}%"

            label = (
                f"#{i} · {ev['home_team']} vs {ev['away_team']}  ·  "
                f"{bet['outcome']}  ·  {mk}  ·  {badge}  ·  starts in {countdown}"
            )

            with st.expander(label):
                st.caption(sport)

                ec1, ec2, ec3, ec4 = st.columns(4)
                ec1.metric("Book", bet["book"])
                ec2.metric("Odds", fmt_odds(bet["price"]))
                ec3.metric("Edge", f"+{ev_pct:.2f}%")
                ec4.metric("Books compared", bet["num_books"])

                fair_odds_impl = bet["consensus"]
                fair_american = int(round((100 / fair_odds_impl) - 100)) if fair_odds_impl < 0.5 else int(round(-100 * fair_odds_impl / (1 - fair_odds_impl)))
                st.caption(
                    f"Market consensus implied prob: **{fair_odds_impl*100:.1f}%**  ·  "
                    f"This book's implied prob: **{bet['implied']*100:.1f}%**  ·  "
                    f"Edge: **{ev_pct:.2f}%**"
                )

                # Kelly stake for EV+ bets
                if bankroll > 0:
                    kelly_frac = (ev_pct / 100)
                    kelly_stake_ev = bankroll * kelly_frac
                    st.caption(f"💡 Kelly stake: **${kelly_stake_ev:.2f}** ({ev_pct:.2f}% of ${bankroll:,.0f} bankroll)")


# ══════════════════════════════════════════════════════════════════════════
# PRIZEPICKS TAB
# ══════════════════════════════════════════════════════════════════════════

with tab_pp:
    st.subheader("🏆 PrizePicks EV+ Picks")
    st.caption(
        "Fetches live PrizePicks lines (free — no API key needed) then compares them "
        "against sportsbook consensus odds. A pick is **+EV** when sportsbooks imply "
        ">50% probability for that outcome, meaning you're getting better-than-fair "
        "value on PrizePicks."
    )
    st.divider()

    pp_hdr, pp_btn_col = st.columns([4, 1])
    with pp_btn_col:
        fetch_pp_btn = st.button("⬇ Fetch Lines", type="primary", use_container_width=True)

    if fetch_pp_btn:
        with st.spinner("Fetching PrizePicks projections…"):
            projs = get_prizepicks_projections()
            st.session_state.pp_projections = projs

    projs = st.session_state.get("pp_projections", None)

    if projs is None:
        st.info("Click **Fetch Lines** to load current PrizePicks projections.")
    elif not projs:
        st.error(
            "Could not fetch PrizePicks projections — their API may be temporarily "
            "unavailable or blocking automated requests. Try again in a few minutes."
        )
    else:
        # ── Filters ──────────────────────────────────────────────────────
        fc1, fc2 = st.columns(2)
        leagues  = sorted(set(p["league"] for p in projs if p["league"]))
        stats    = sorted(set(p["stat_type"] for p in projs if p["stat_type"]))

        with fc1:
            sel_league = st.selectbox("League", ["All"] + leagues, key="pp_league")
        with fc2:
            sel_stat = st.selectbox("Stat type", ["All"] + stats, key="pp_stat")

        filtered = projs
        if sel_league != "All":
            filtered = [p for p in filtered if p["league"] == sel_league]
        if sel_stat != "All":
            filtered = [p for p in filtered if p["stat_type"] == sel_stat]

        st.caption(f"Showing {len(filtered)} of {len(projs)} projections")

        # ── Projections table ─────────────────────────────────────────────
        if filtered:
            st.dataframe(
                [
                    {
                        "Player":  p["player"],
                        "Team":    p["team"],
                        "League":  p["league"],
                        "Stat":    p["stat_type"],
                        "PP Line": p["line"],
                    }
                    for p in filtered[:100]
                ],
                use_container_width=True,
                hide_index=True,
            )

        st.divider()

        # ── EV+ finder ───────────────────────────────────────────────────
        st.markdown("**Find EV+ Picks**")
        st.caption(
            "⚠️ Uses Odds API credits to look up sportsbook player prop odds. "
            "Results are cached — repeated clicks within 15 min are free."
        )

        ev_c1, ev_c2 = st.columns([3, 1])
        with ev_c1:
            min_pp_edge = st.slider("Min edge %", 1.0, 15.0, 3.0, 0.5, key="pp_min_edge")
        with ev_c2:
            find_ev_pp_btn = st.button("🔍 Find EV+", key="pp_ev_btn", use_container_width=True)

        if find_ev_pp_btn:
            supported = [p for p in filtered if p["league"] in PRIZEPICKS_LEAGUE_SPORT]
            if not supported:
                st.warning(
                    "No supported league found in current filter. "
                    "Supported leagues: NBA, NFL, MLB, NHL."
                )
            else:
                with st.spinner(
                    f"Comparing {len(supported)} props against sportsbook odds…"
                ):
                    ev_picks = find_prizepicks_ev(supported, min_edge=min_pp_edge)
                    st.session_state.pp_ev_picks = ev_picks

        ev_picks = st.session_state.get("pp_ev_picks", [])

        if ev_picks:
            st.divider()
            ep1, ep2 = st.columns(2)
            ep1.metric("EV+ picks found", len(ev_picks))
            ep2.metric("Best edge", f"+{ev_picks[0]['edge']:.1f}%")
            st.divider()

            for i, pick in enumerate(ev_picks, 1):
                edge = pick["edge"]
                if edge >= 8:
                    badge = f"🟢 +{edge:.1f}%"
                elif edge >= 5:
                    badge = f"🟡 +{edge:.1f}%"
                else:
                    badge = f"🔴 +{edge:.1f}%"

                label = (
                    f"#{i} · {pick['player']}  ·  "
                    f"{pick['stat_type']} **{pick['direction']}** {pick['line']}  ·  "
                    f"{badge}  ·  {pick['league']}"
                )

                with st.expander(label):
                    pc1, pc2, pc3, pc4 = st.columns(4)
                    pc1.metric("PP Line",    pick["line"])
                    pc2.metric("Pick",       pick["direction"])
                    pc3.metric("Best SB Odds", fmt_odds(pick["sb_price"]))
                    pc4.metric("SB Book",    pick["sb_book"])

                    st.caption(
                        f"Sportsbook implied prob: **{pick['sb_implied']*100:.1f}%** "
                        f"vs PrizePicks break-even (~50%) → Edge: **+{edge:.1f}%**"
                    )

                    if bankroll > 0:
                        kelly_pp = bankroll * (edge / 100)
                        st.caption(
                            f"💡 Kelly stake: **${kelly_pp:.2f}** "
                            f"({edge:.1f}% of ${bankroll:,.0f} bankroll)"
                        )
