#!/usr/bin/env python3
"""
Arb Scanner v4
- Auto-scans sportsbooks via The Odds API
- Multi-leg PrizePicks / Underdog prop arb (2-6 legs)

Multi-leg arb logic:
- You enter all legs of a DFS entry (2-6 picks)
- Scanner auto-fetches sportsbook odds for each leg
- Only legs where sportsbook odds are favorable enough get hedged
- Hedge stake is split evenly across all hedged legs
- Shows: which legs to hedge, what to bet, guaranteed floor vs max upside

Optimizations:
- Parallel sport scans via ThreadPoolExecutor
- In-memory cache for event props
- Team-name filtering for prop lookups
- NOW computed fresh per call (no drift)
- Implied probs stored at detection time
"""

import os
import time
import requests
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

API_KEY = os.environ.get("ODDS_API_KEY", "")
MIN_MARGIN = 2.0

PRIORITY_SPORTS = [
    "basketball_nba",
    "icehockey_nhl",
    "baseball_mlb",
    "soccer_epl",
    "soccer_usa_mls",
    "soccer_uefa_champs_league",
    "mma_mixed_martial_arts",
    "basketball_ncaab",
    "americanfootball_nfl",
]

BASE_URL = "https://api.the-odds-api.com"
EST = timezone(timedelta(hours=-5))

_prop_cache = {}
_events_cache = {}  # {sport: events_list} — reused within a session to save quota
_api_cache = {}     # {(sport, markets): (fetch_time, data)} — 15-min TTL
_CACHE_TTL = 900    # seconds (15 minutes)
_print_lock = threading.Lock()

PLATFORM_MULTIPLIERS = {
    "PrizePicks": {2: 3.0, 3: 5.0, 4: 10.0, 5: 20.0, 6: 40.0},
    "Underdog":   {2: 3.0, 3: 6.0, 4: 10.0, 5: 20.0},
}

PROP_MARKET_MAP = {
    "points":     "player_points",
    "rebounds":   "player_rebounds",
    "assists":    "player_assists",
    "threes":     "player_threes",
    "strikeouts": "player_strikeouts",
    "hits":       "player_hits",
    "goals":      "player_goals_scored",
    "saves":      "player_saves",
    "shots":      "player_shots_on_target",
}

SPORT_MAP = {
    "nba": "basketball_nba",
    "nfl": "americanfootball_nfl",
    "mlb": "baseball_mlb",
    "nhl": "icehockey_nhl",
}

# ── helpers ────────────────────────────────────────────────────────────────

def to_implied(american):
    if american > 0:
        return 100 / (american + 100)
    return abs(american) / (abs(american) + 100)

def to_payout(stake, american):
    if american > 0:
        return stake * (1 + american / 100)
    return stake * (1 + 100 / abs(american))

def fmt_odds(o):
    return f"+{o}" if o > 0 else str(o)

def time_until(dt_str):
    now = datetime.now(EST)
    game_time = datetime.fromisoformat(dt_str.replace("Z", "+00:00")).astimezone(EST)
    diff = game_time - now
    total_minutes = int(diff.total_seconds() / 60)
    if total_minutes < 0:
        return "started"
    elif total_minutes < 60:
        return f"{total_minutes}m"
    hours = total_minutes // 60
    mins = total_minutes % 60
    return f"{hours}h {mins}m"

def divider(char="=", width=68):
    print(char * width)

# ── API calls ──────────────────────────────────────────────────────────────

def get_odds(sport, markets="h2h,spreads,totals", quiet=False):
    cache_key = (sport, markets)
    now = time.time()

    # Return cached data if still fresh
    if cache_key in _api_cache:
        ts, cached_data = _api_cache[cache_key]
        if now - ts < _CACHE_TTL:
            return cached_data

    r = requests.get(
        f"{BASE_URL}/v4/sports/{sport}/odds/",
        params={
            "apiKey": API_KEY,
            "regions": "us",
            "markets": markets,
            "oddsFormat": "american",
            "dateFormat": "iso",
        },
        timeout=15,
    )
    if r.status_code == 422:
        _api_cache[cache_key] = (now, [])
        return []
    r.raise_for_status()
    if not quiet:
        remaining = r.headers.get("x-requests-remaining", "?")
        with _print_lock:
            print(f"  [{remaining} requests remaining]")
    data = r.json()
    _api_cache[cache_key] = (now, data)
    return data

def get_event_props(sport, event_id, market):
    cache_key = (sport, event_id, market)
    if cache_key in _prop_cache:
        return _prop_cache[cache_key]
    r = requests.get(
        f"{BASE_URL}/v4/sports/{sport}/events/{event_id}/odds",
        params={
            "apiKey": API_KEY,
            "regions": "us",
            "markets": market,
            "oddsFormat": "american",
        },
        timeout=15,
    )
    if r.status_code in (422, 404):
        _prop_cache[cache_key] = None
        return None
    r.raise_for_status()
    result = r.json()
    _prop_cache[cache_key] = result
    return result

# ── sportsbook prop lookup ─────────────────────────────────────────────────

def get_sportsbook_prop_odds(player_name, stat_type, line, sport, team_hint=None):
    """Return (best_over, best_under) dicts with price+book, or None."""
    market = PROP_MARKET_MAP.get(stat_type.lower())
    if not market:
        return None, None

    try:
        if sport not in _events_cache:
            _events_cache[sport] = get_odds(sport, markets="h2h", quiet=True)
        events = _events_cache[sport]
    except Exception:
        return None, None

    if not events:
        return None, None

    if team_hint:
        hint = team_hint.lower()
        matched = [
            ev for ev in events
            if hint in ev.get("home_team", "").lower()
            or hint in ev.get("away_team", "").lower()
        ]
        events_to_search = matched if matched else events
    else:
        events_to_search = events

    best_over = None
    best_under = None

    for ev in events_to_search:
        try:
            prop_data = get_event_props(sport, ev["id"], market)
            if not prop_data:
                continue
            for bm in prop_data.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    for oc in mkt.get("outcomes", []):
                        name_match = player_name.lower() in oc.get("description", "").lower()
                        line_match = oc.get("point") == line
                        if name_match and line_match:
                            if oc["name"] == "Over":
                                if best_over is None or oc["price"] > best_over["price"]:
                                    best_over = {"price": oc["price"], "book": bm["title"]}
                            elif oc["name"] == "Under":
                                if best_under is None or oc["price"] > best_under["price"]:
                                    best_under = {"price": oc["price"], "book": bm["title"]}
        except Exception:
            continue

    return best_over, best_under

# ── game arb detection ─────────────────────────────────────────────────────

def find_arbs(events):
    arbs = []
    for ev in events:
        markets = {}
        for bm in ev.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                mk = mkt["key"]
                if mk not in markets:
                    markets[mk] = {}
                for oc in mkt["outcomes"]:
                    if mk == "totals":
                        key = f"{oc['name']} {oc.get('point', '')}"
                    elif mk == "spreads":
                        key = f"{oc['name']}|{oc.get('point', '')}"
                    else:
                        key = oc["name"]
                    if key not in markets[mk] or markets[mk][key]["price"] < oc["price"]:
                        markets[mk][key] = {
                            "price": oc["price"],
                            "book": bm["title"],
                            "point": oc.get("point"),
                            "implied": to_implied(oc["price"]),
                        }

        for mk, ocs in markets.items():
            keys = list(ocs.keys())

            if mk == "spreads":
                # Group sides by abs(point) — home -3.5 pairs with away +3.5
                by_pt = {}
                for k in keys:
                    pt = abs(ocs[k]["point"] or 0)
                    by_pt.setdefault(pt, []).append(k)
                for pt, group in by_pt.items():
                    # Try every pair in the group; keep best-margin arb at this spread
                    best = None
                    for i in range(len(group)):
                        for j in range(i + 1, len(group)):
                            a, b = ocs[group[i]], ocs[group[j]]
                            if a["book"] == b["book"]:
                                continue
                            s = a["implied"] + b["implied"]
                            m = (1 - s) * 100
                            if m >= MIN_MARGIN and (best is None or m > best["margin"]):
                                best = {"ev": ev, "mk": mk, "ocs": {group[i]: a, group[j]: b}, "sum": s, "margin": m}
                    if best:
                        arbs.append(best)

            elif mk == "totals":
                # Group by point value so "Over 220.5" only pairs with "Under 220.5"
                by_pt = {}
                for k in keys:
                    parts = k.split(" ", 1)
                    pt = parts[1] if len(parts) > 1 else ""
                    by_pt.setdefault(pt, {})[k] = ocs[k]
                for pt_ocs in by_pt.values():
                    ov = next((k for k in pt_ocs if k.startswith("Over")), None)
                    un = next((k for k in pt_ocs if k.startswith("Under")), None)
                    if not ov or not un or pt_ocs[ov]["book"] == pt_ocs[un]["book"]:
                        continue
                    s = pt_ocs[ov]["implied"] + pt_ocs[un]["implied"]
                    margin = (1 - s) * 100
                    if margin >= MIN_MARGIN:
                        arbs.append({"ev": ev, "mk": mk, "ocs": {ov: pt_ocs[ov], un: pt_ocs[un]}, "sum": s, "margin": margin})

            elif len(keys) == 2:
                k1, k2 = keys[0], keys[1]
                if ocs[k1]["book"] == ocs[k2]["book"]:
                    continue
                s = ocs[k1]["implied"] + ocs[k2]["implied"]
                margin = (1 - s) * 100
                if margin >= MIN_MARGIN:
                    arbs.append({"ev": ev, "mk": mk, "ocs": ocs, "sum": s, "margin": margin})

            elif len(keys) == 3:
                books = {ocs[k]["book"] for k in keys}
                if len(books) < 2:
                    continue
                s = sum(ocs[k]["implied"] for k in keys)
                margin = (1 - s) * 100
                if margin >= MIN_MARGIN:
                    arbs.append({"ev": ev, "mk": mk, "ocs": ocs, "sum": s, "margin": margin})

    return arbs

def scan_sport(sport):
    try:
        events = get_odds(sport)
        found = find_arbs(events)
        with _print_lock:
            print(f"  {sport.replace('_', ' ')}: {len(events)} events, {len(found)} arbs")
        return events, found
    except Exception as e:
        with _print_lock:
            print(f"  {sport.replace('_', ' ')}: Error — {e}")
        return [], []

def scan_all_sports(sports):
    all_arbs = []
    total_events = 0
    _events_cache.clear()  # refresh so prop lookups this session get current odds
    print(f"Scanning {len(sports)} sports in parallel...\n")
    with ThreadPoolExecutor(max_workers=min(len(sports), 9)) as executor:
        futures = {executor.submit(scan_sport, sport): sport for sport in sports}
        for future in as_completed(futures):
            events, found = future.result()
            total_events += len(events)
            all_arbs.extend(found)
    return all_arbs, total_events

# ── game arb display ───────────────────────────────────────────────────────

def print_game_summary(arbs):
    mk_labels = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}
    print(f"\n  {'#':<4} {'EVENT':<32} {'MARKET':<12} {'MARGIN':<10} {'STARTS IN'}")
    print(f"  {'-'*4} {'-'*32} {'-'*12} {'-'*10} {'-'*10}")
    for i, arb in enumerate(arbs, 1):
        ev = arb["ev"]
        name = f"{ev['home_team']} vs {ev['away_team']}"
        if len(name) > 31:
            name = name[:28] + "..."
        mk = mk_labels.get(arb["mk"], arb["mk"])
        countdown = time_until(ev["commence_time"])
        print(f"  {i:<4} {name:<32} {mk:<12} {arb['margin']:.2f}%{'':4} {countdown}")

def print_game_slip(arb, stake, arb_num):
    ev = arb["ev"]
    mk_label = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}.get(arb["mk"], arb["mk"])
    est_time = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")).astimezone(EST)
    countdown = time_until(ev["commence_time"])

    print(f"\n  ARB #{arb_num}  —  {mk_label}  —  {arb['margin']:.2f}% margin")
    print(f"  {ev['home_team']} vs {ev['away_team']}")
    print(f"  {ev['sport_key'].replace('_', ' ')}  |  {est_time.strftime('%b %d %I:%M %p ET')}  |  starts in {countdown}")
    print(f"  Total stake: ${stake:.2f}\n")

    by_book = {}
    bets = []
    for name, o in arb["ocs"].items():
        st = stake * (o["implied"] / arb["sum"])
        pay = to_payout(st, o["price"])
        bets.append((name, o, st, pay))
        by_book.setdefault(o["book"], []).append((name, o, st))

    guaranteed = min(b[3] for b in bets) - stake

    print(f"  {'SPORTSBOOK':<22} {'BET ON':<26} {'ODDS':<8} {'STAKE'}")
    print(f"  {'-'*22} {'-'*26} {'-'*8} {'-'*10}")
    for book, book_bets in by_book.items():
        for name, o, st in book_bets:
            display = name.split("|")[0] if "|" in name else name
            if len(display) > 25:
                display = display[:22] + "..."
            print(f"  {book:<22} {display:<26} {fmt_odds(o['price']):<8} ${st:.2f}")

    print(f"\n  Guaranteed profit: ${guaranteed:.2f}  (regardless of outcome)")
    print(f"  Payout if any leg wins: ~${bets[0][3]:.2f}")
    return guaranteed

# ── multi-leg prop arb ─────────────────────────────────────────────────────

def collect_legs(platform_key, num_legs):
    """Prompt user to enter each leg of their DFS entry. Returns list of leg dicts."""
    legs = []
    print(f"\n  Enter each of your {num_legs} legs:\n")
    for i in range(1, num_legs + 1):
        print(f"  --- Leg {i} of {num_legs} ---")
        player = input("    Player name (e.g. LeBron James): ").strip()
        if not player:
            print("    Skipping remaining legs.")
            break

        team = input("    Team (e.g. Lakers) — helps narrow search: ").strip()

        stat = input(f"    Stat ({'/'.join(PROP_MARKET_MAP.keys())}): ").strip().lower()
        if stat not in PROP_MARKET_MAP:
            print(f"    Unrecognized stat. Supported: {', '.join(PROP_MARKET_MAP.keys())}")
            break

        try:
            line = float(input("    Line (e.g. 25.5): ").strip())
        except ValueError:
            print("    Invalid line.")
            break

        direction_input = input("    Your pick — Over or Under? ").strip().lower()
        if direction_input not in ("over", "under"):
            print("    Enter 'over' or 'under'.")
            break
        direction = direction_input.capitalize()

        sport_input = input("    Sport (nba / nfl / mlb / nhl): ").strip().lower()
        sport = SPORT_MAP.get(sport_input)
        if not sport:
            print("    Unrecognized sport.")
            break

        legs.append({
            "player": player,
            "team": team or None,
            "stat": stat,
            "line": line,
            "direction": direction,
            "sport": sport,
        })
        print()

    return legs

def analyze_multileg_entry(legs, platform_key, num_legs, total_stake):
    """
    For each leg, fetch the sportsbook opposite and check if the margin is favorable.
    Only hedge legs where margin >= MIN_MARGIN.
    Hedge stake is split evenly across all hedged legs.
    
    Returns a result dict with full breakdown.
    """
    multiplier = PLATFORM_MULTIPLIERS[platform_key][num_legs]
    dfs_payout = total_stake * multiplier  # what you collect if ALL legs hit

    print(f"\n  Fetching sportsbook odds for all {len(legs)} legs...\n")

    leg_results = []

    for i, leg in enumerate(legs, 1):
        print(f"  Leg {i}: {leg['player']} {leg['stat']} {leg['direction']} {leg['line']}...")
        best_over, best_under = get_sportsbook_prop_odds(
            leg["player"], leg["stat"], leg["line"], leg["sport"],
            team_hint=leg["team"]
        )

        # The sportsbook hedge is the opposite of the DFS pick
        hedge_direction = "Under" if leg["direction"] == "Over" else "Over"
        hedge_odds_data = best_under if hedge_direction == "Under" else best_over

        if not hedge_odds_data:
            print(f"    No {hedge_direction} odds found on sportsbooks — cannot hedge this leg.")
            leg_results.append({
                **leg,
                "hedge_direction": hedge_direction,
                "hedge_odds": None,
                "hedge_book": None,
                "margin": None,
                "hedgeable": False,
                "reason": "No sportsbook odds found",
            })
            continue

        sb_price = hedge_odds_data["price"]
        sb_implied = to_implied(sb_price)

        # Per-leg arb margin: DFS leg implied prob is 1/multiplier (treated as fair single-leg odds)
        # This represents: if this ONE leg loses on DFS (and we hedge it), what's our edge?
        # We use the leg's standalone implied prob on the sportsbook side vs 50/50 DFS leg fair value.
        # More precisely: for the hedge to be worth placing, the sportsbook implied on the
        # opposite side must be favorable enough relative to the true probability.
        dfs_leg_implied = 0.5  # DFS doesn't give explicit per-leg odds; fair value assumed ~50%
        arb_sum = dfs_leg_implied + sb_implied
        margin = (1 - arb_sum) * 100

        hedgeable = margin >= MIN_MARGIN

        if hedgeable:
            print(f"    ✓ Hedge available: {hedge_direction} @ {fmt_odds(sb_price)} on {hedge_odds_data['book']}  ({margin:.2f}% margin)")
        else:
            print(f"    ✗ Margin too thin: {hedge_direction} @ {fmt_odds(sb_price)} on {hedge_odds_data['book']}  ({margin:.2f}% — need {MIN_MARGIN}%)")

        leg_results.append({
            **leg,
            "hedge_direction": hedge_direction,
            "hedge_odds": sb_price,
            "hedge_book": hedge_odds_data["book"],
            "sb_implied": sb_implied,
            "margin": margin,
            "hedgeable": hedgeable,
            "reason": None,
        })

    return leg_results, multiplier, dfs_payout

def print_multileg_slip(leg_results, platform_key, num_legs, total_stake, multiplier, dfs_payout):
    """
    Print the full betting slip for a multi-leg DFS entry with selective hedging.

    Strategy:
    - DFS entry: place full stake, hope all legs hit → collect multiplier payout
    - Hedged legs: place equal sportsbook bets on the OPPOSITE outcome for each favorable leg
      so that if any hedged leg misses on DFS, the sportsbook bet partially offsets the loss
    - Unhedged legs: accepted as pure DFS risk (margin too thin to hedge profitably)
    """
    hedged = [l for l in leg_results if l["hedgeable"]]
    unhedged = [l for l in leg_results if not l["hedgeable"]]

    divider()
    print(f"  MULTI-LEG PROP ARB — {platform_key}  |  {num_legs} legs  |  {multiplier}x")
    divider()
    print(f"\n  DFS entry stake:  ${total_stake:.2f}")
    print(f"  DFS payout if ALL hit:  ${dfs_payout:.2f}  (net +${dfs_payout - total_stake:.2f})")
    print(f"\n  Legs to hedge: {len(hedged)} of {len(leg_results)}")
    if unhedged:
        print(f"  Unhedged legs (margin too thin): {len(unhedged)}")

    if not hedged:
        print("\n  No legs meet the minimum margin threshold for hedging.")
        print("  Either play the entry straight or skip it.\n")
        return

    # Hedge stake split evenly across all hedged legs
    hedge_stake_per_leg = total_stake / len(hedged)
    total_hedge_stake = hedge_stake_per_leg * len(hedged)

    print(f"\n  Hedge stake per leg: ${hedge_stake_per_leg:.2f}  (evenly split)")
    print(f"  Total hedge stake:   ${total_hedge_stake:.2f}")
    print(f"  Combined stake (DFS + all hedges): ${total_stake + total_hedge_stake:.2f}\n")

    # Print the DFS slip
    print(f"  {'─'*64}")
    print(f"  DFS ENTRY — {platform_key}  (stake: ${total_stake:.2f})")
    print(f"  {'─'*64}")
    for i, l in enumerate(leg_results, 1):
        status = "HEDGE ✓" if l["hedgeable"] else "no hedge"
        print(f"  Leg {i}: {l['player']:<22} {l['stat']:<12} {l['direction']:<6} {l['line']}   [{status}]")

    # Print the sportsbook hedge slip
    print(f"\n  {'─'*64}")
    print(f"  SPORTSBOOK HEDGES  (${hedge_stake_per_leg:.2f} each)")
    print(f"  {'─'*64}")
    print(f"  {'SPORTSBOOK':<22} {'PLAYER':<20} {'BET':<22} {'ODDS':<8} {'STAKE':<10} {'PAYOUT'}")
    print(f"  {'-'*22} {'-'*20} {'-'*22} {'-'*8} {'-'*10} {'-'*10}")

    hedge_payouts = []
    for l in hedged:
        bet_desc = f"{l['hedge_direction']} {l['line']}"
        payout = to_payout(hedge_stake_per_leg, l["hedge_odds"])
        hedge_payouts.append(payout)
        print(f"  {l['hedge_book']:<22} {l['player'][:19]:<20} {bet_desc:<22} {fmt_odds(l['hedge_odds']):<8} ${hedge_stake_per_leg:<9.2f} ${payout:.2f}")

    # ── Outcome scenarios ──
    print(f"\n  {'─'*64}")
    print(f"  OUTCOME SCENARIOS")
    print(f"  {'─'*64}")

    # Best case: all DFS legs hit, all hedge bets lose
    # Net = DFS payout - DFS stake - total hedge stake (hedge bets lost)
    best_case_net = dfs_payout - total_stake - total_hedge_stake
    print(f"  All {num_legs} legs HIT on DFS:")
    print(f"    DFS payout ${dfs_payout:.2f} − entry ${total_stake:.2f} − hedges lost ${total_hedge_stake:.2f}")
    print(f"    Net: ${best_case_net:+.2f}")

    # Partial hedge scenario: for each hedged leg that misses, collect that hedge payout
    # Show the floor: all DFS legs miss (worst case), all hedge bets win
    # This is the "at least I get back X" floor
    all_hedges_win = sum(hedge_payouts)
    worst_case_net = all_hedges_win - total_stake - total_hedge_stake
    print(f"\n  All DFS legs MISS (hedges all win):")
    print(f"    Hedge payouts ${all_hedges_win:.2f} − entry ${total_stake:.2f} − hedge stakes ${total_hedge_stake:.2f}")
    print(f"    Net: ${worst_case_net:+.2f}")

    # Most likely partial scenario: some hit, some miss
    if len(hedged) > 0 and len(unhedged) > 0:
        print(f"\n  Note: {len(unhedged)} unhedged leg(s) are pure DFS risk.")
        print(f"  If any unhedged leg misses, the full DFS entry loses with no sportsbook offset.")

    print(f"\n  {'─'*64}")
    if best_case_net > 0:
        print(f"  Best case (all hit):  +${best_case_net:.2f}")
    else:
        print(f"  Best case (all hit):  ${best_case_net:.2f}  ← DFS payout doesn't cover hedge cost")
    print(f"  Floor (all miss, hedges win): ${worst_case_net:+.2f}")
    print(f"  {'─'*64}\n")

    return best_case_net, worst_case_net, total_stake + total_hedge_stake

def prop_arb_calculator():
    print("\n  HOW MULTI-LEG PROP ARB WORKS")
    print("  ─────────────────────────────────────────────────────────────")
    print("  Enter your full DFS entry (2-6 legs).")
    print("  The scanner fetches sportsbook odds for the opposite side of")
    print("  each leg. Legs with favorable margins get a sportsbook hedge")
    print("  (stake split evenly). Thin-margin legs are left unhedged.")
    print("  ─────────────────────────────────────────────────────────────\n")

    results = []

    while True:
        platform = input("  Platform (prizepicks / underdog, or Enter to finish): ").strip().lower()
        if platform == "":
            break
        if platform not in ("prizepicks", "underdog"):
            print("  Enter 'prizepicks' or 'underdog'.")
            continue
        platform_key = "PrizePicks" if platform == "prizepicks" else "Underdog"

        multipliers = PLATFORM_MULTIPLIERS[platform_key]
        legs_input = input(f"  How many legs? ({'/'.join(str(k) for k in multipliers)}): ").strip()
        try:
            num_legs = int(legs_input)
        except ValueError:
            print("  Invalid number.")
            continue
        if num_legs not in multipliers:
            print(f"  {platform_key} doesn't support {num_legs} legs. Options: {list(multipliers.keys())}")
            continue

        multiplier = multipliers[num_legs]
        print(f"  {num_legs}-leg entry pays {multiplier}x if all hit.\n")

        try:
            total_stake = float(input("  Total DFS entry stake ($): $").strip())
        except ValueError:
            print("  Invalid stake.")
            continue

        # Collect all legs
        legs = collect_legs(platform_key, num_legs)

        if len(legs) < 2:
            print("  Need at least 2 legs to evaluate. Skipping.\n")
            continue

        # Analyze and print slip
        leg_results, multiplier, dfs_payout = analyze_multileg_entry(legs, platform_key, num_legs, total_stake)

        outcome = print_multileg_slip(leg_results, platform_key, num_legs, total_stake, multiplier, dfs_payout)

        if outcome:
            best_case, floor, total_committed = outcome
            results.append({
                "platform": platform_key,
                "legs": num_legs,
                "stake": total_committed,
                "best_case": best_case,
                "floor": floor,
            })

        another = input("  Check another entry? (y/n): ").strip().lower()
        if another != "y":
            break
        print()

    return results

# ── stake prompt ───────────────────────────────────────────────────────────

def get_stake_for_arb(arb_num, event_name, margin):
    while True:
        try:
            val = input(f"  Stake for arb #{arb_num} ({event_name}, {margin:.2f}% margin): $").strip()
            if val == "":
                print("  Skipping.\n")
                return None
            stake = float(val)
            if stake <= 0:
                print("  Please enter a positive amount.")
                continue
            return stake
        except ValueError:
            print("  Invalid amount. Enter a number or press Enter to skip.")

# ── main ───────────────────────────────────────────────────────────────────

def main():
    print("\n" + "="*68)
    print("  ARB SCANNER v4")
    print("  Sportsbook arbs  +  Multi-leg PrizePicks / Underdog prop arbs")
    print("="*68)

    print("\nWhat do you want to scan?\n")
    print("  1 — Sportsbook arbs (moneyline, spread, totals)")
    print("  2 — PrizePicks / Underdog multi-leg prop arbs")
    print("  3 — Both\n")

    choice = input("  Enter 1, 2, or 3: ").strip()

    total_staked = 0
    total_best_case = 0

    # ── SPORTSBOOK SCAN ──
    if choice in ("1", "3"):
        print(f"\n\n{'='*68}")
        print("  SPORTSBOOK SCAN")
        print(f"{'='*68}\n")
        print(f"Scanning for game arbs with >= {MIN_MARGIN}% margin...\n")

        all_arbs, total_events = scan_all_sports(PRIORITY_SPORTS)
        print(f"\nScan complete: {total_events} events, {len(all_arbs)} arbs found")

        if all_arbs:
            all_arbs.sort(key=lambda a: a["margin"], reverse=True)

            print(f"\n{'='*68}")
            print(f"  SUMMARY — {len(all_arbs)} game arb(s)")
            print(f"{'='*68}")
            print_game_summary(all_arbs)
            print(f"{'='*68}\n")

            print("Enter your stake for each arb (press Enter to skip):\n")
            chosen = []
            for i, arb in enumerate(all_arbs, 1):
                ev = arb["ev"]
                short_name = f"{ev['home_team']} vs {ev['away_team']}"
                if len(short_name) > 35:
                    short_name = short_name[:32] + "..."
                stake = get_stake_for_arb(i, short_name, arb["margin"])
                if stake is not None:
                    chosen.append((arb, stake))

            if chosen:
                print(f"\n{'='*68}")
                print("  BETTING SLIPS — GAME ARBS")
                print(f"{'='*68}")
                for i, (arb, stake) in enumerate(chosen, 1):
                    profit = print_game_slip(arb, stake, i)
                    total_staked += stake
                    total_best_case += profit
                    print()
        else:
            print("\nNo game arbs found right now. Try again in 15-30 minutes.")

    # ── PROP SCAN ──
    if choice in ("2", "3"):
        print(f"\n\n{'='*68}")
        print("  PRIZEPICKS / UNDERDOG MULTI-LEG PROP ARB")
        print(f"{'='*68}")
        prop_results = prop_arb_calculator()
        for r in prop_results:
            total_staked += r["stake"]
            total_best_case += r["best_case"]

    # ── FINAL SUMMARY ──
    if total_staked > 0:
        print(f"\n{'='*68}")
        print("  SESSION SUMMARY")
        print(f"{'='*68}")
        print(f"  Total committed:         ${total_staked:.2f}")
        print(f"  Best case net profit:    ${total_best_case:+.2f}")
        print(f"{'='*68}")

    print("\n  Act fast — arb windows close within minutes.")
    print("  Sportsbooks may limit accounts suspected of arbing.")
    print("  Always verify odds are still live before placing.\n")


if __name__ == "__main__":
    main()
