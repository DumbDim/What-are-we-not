"""Main entry point: run the smart dispatcher on a practice world or the final."""
from __future__ import annotations
import argparse
import os
import sys
import time

# Load .env if present
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

from spread.client import Client, SpreadError, Unreachable, World
from spread.types import DeskEventKind, Score
from dispatcher.routing import Router
from dispatcher.pricing import Pricer
from dispatcher.drivers import DriverManager
from dispatcher.desk import SmartDesk, WAKE_ON, WEEKDAYS, DAY

HOUR = 60


def report(label: str, score: Score) -> None:
    orders = f"{score['wonOrders']:>4} orders  {score['onTimeShare']:6.1%} on time  {score['undeliveredShare']:5.1%} undelivered"
    margin_eur = score["marginCents"] / 100
    revenue_eur = score["revenueCents"] / 100
    margin_pct = f"{score['marginShare']:5.1%}" if score["revenueCents"] > 0 else "  N/A"
    print(f"{label:<9}  margin €{margin_eur:>11,.2f} ({margin_pct})  revenue €{revenue_eur:>11,.2f}  {orders}", flush=True)


def play_practice(world: World, level: int, router: Router) -> None:
    """Step through a practice world with our smart dispatcher."""
    pricer = Pricer(level)
    driver_mgr = DriverManager()
    driver_mgr.load_roster(world.roster())

    snapshot = world.snapshot()
    desk = SmartDesk(world, level, snapshot["now"], router, pricer, driver_mgr)
    ends_at = snapshot["endsAt"]

    while desk.now < ends_at:
        day = desk.now // DAY
        midnight = (desk.now // DAY + 1) * DAY

        desk.maintain()
        actions, desk.outbox = desk.outbox, []
        step = world.advance(until=WAKE_ON, max=min(ends_at, midnight, desk.next_wake()), actions=actions)
        desk.read_results(actions, step["results"])
        desk.now = step["now"]

        for event in step["events"]:
            desk.handle(event)

        if desk.now // DAY > day:
            report(WEEKDAYS[day % 7], world.score())


def play_final(world: World, router: Router) -> None:
    """Play the final with event streaming and restart recovery."""
    history = world.events()
    snapshot = world.snapshot()
    if snapshot["now"] >= snapshot["endsAt"]:
        return

    level = next((w["level"] for w in world.client.worlds() if w["worldId"] == world.id), 8)
    pricer = Pricer(level)
    driver_mgr = DriverManager()
    driver_mgr.load_roster(world.roster())

    desk = SmartDesk(world, level, snapshot["now"], router, pricer, driver_mgr)
    desk.rejoin(snapshot, history)
    desk.send()

    for event in world.stream(after=history[-1]["seq"] if history else -1, idle=1.0):
        day = desk.now // DAY
        desk.now = world.client.final()["now"] if event is None else max(desk.now, event["at"])
        if event is not None:
            desk.handle(event)
        desk.maintain()
        desk.send()
        if desk.now // DAY > day:
            report(WEEKDAYS[day % 7], world.score())
        if desk.now >= snapshot["endsAt"]:
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="Junction Smart Dispatcher for Spread")
    parser.add_argument("--level", type=int, choices=range(1, 9), default=8, help="practice level (default 8)")
    parser.add_argument("--seed", help="seed for deterministic world")
    parser.add_argument("--board", action="store_true", help="ranking run")
    parser.add_argument("--final", action="store_true", help="play the final")
    args = parser.parse_args()

    try:
        client = Client()
        print("Connected to Spread server.", flush=True)

        # Precompute the map
        print("Loading map...", end=" ", flush=True)
        router = Router(client.map())
        print(f"{len(router.place_ids)} places loaded.", flush=True)

        if args.final:
            world = None
            while True:
                try:
                    if world is None:
                        from spread.starter import next_final
                        world = next_final(client)
                        print(f"Final world {world.id}.", flush=True)
                    play_final(world, router)
                    break
                except Unreachable as error:
                    print(f"spread: {error}. Retrying in 5s.", file=sys.stderr, flush=True)
                    time.sleep(5)
        else:
            world = client.create(level=args.level, seed=args.seed, board=args.board)
            kind = "Ranking" if args.board else "Practice"
            print(f"{kind} world {world.id}, level {args.level}.", flush=True)

            try:
                play_practice(world, args.level, router)
            except BaseException as error:
                try:
                    world.delete()
                    deleted = True
                except SpreadError:
                    deleted = False
                if isinstance(error, KeyboardInterrupt):
                    status = "deleted" if deleted else "not deleted (holds a slot)"
                    sys.exit(f"Stopped. World {status}.")
                print(f"Error. World {'deleted' if deleted else 'not deleted'}.", file=sys.stderr)
                raise

        score = world.score()
        report("End", score)
        if score["ranked"]:
            print("Ranked!")
        elif score["wonOrders"] == 0:
            print("Not ranked: no orders won.")
        else:
            print("Not ranked: below 90% on-time delivery minimum.")

        # Delete practice worlds to free slots
        if not args.final and not args.board:
            world.delete()
            print("World deleted.")

    except SpreadError as error:
        sys.exit(f"spread: {error}")
    except KeyboardInterrupt:
        sys.exit("Stopped.")


if __name__ == "__main__":
    main()
