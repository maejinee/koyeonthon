"""Codex 봇 전략 회귀 검증: 실제 대회 엔진으로 생산·이동·전투·점령을 확인한다.

실행 (koyeon 폴더에서): python3 -m unittest discover -s dev_tools/tests -v
"""
from collections import defaultdict
from pathlib import Path
import sys
import unittest

DEV_TOOLS = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(DEV_TOOLS.parent / "out" / "codex_bot"), str(DEV_TOOLS)]

from engine.commands import DIRECTIONS, Move, Move2, Spawn, Tele
from engine.config import load_config
from engine.pipeline import run_turn, step_move, step_spawn, unit_cost
from engine.state import Building, new_game
from mapgen import generate, to_state
from protocol import parse_init, parse_turn
from runner.protocol import parse_commands, serialize_init, serialize_turn
from strategy import BattleBot


def make_state(buildings, units=None, resources=None):
    state = new_game(
        load_config(), buildings=buildings,
        resources=resources if resources is not None else {"Y": 0, "K": 0},
    )
    state.units = dict(units or {})
    return state


def make_bot(state, team="Y"):
    init = parse_init(serialize_init(state, team).splitlines()[:-1])
    return BattleBot(init)


def decide(bot, state, turn=20):
    view = parse_turn(serialize_turn(state, bot.team, turn).splitlines()[:-1], bot.init)
    return bot.decide(view)


class StrategyTest(unittest.TestCase):
    def assert_commands_executable(self, state, team, lines):
        """엔진의 부분 생산/부분 이동이 잘못된 과다 요청을 숨기지 않게 검사한다."""
        commands = parse_commands(lines)
        self.assertEqual(len(commands), len(lines), "무효한 출력 명령")
        spawns = [c for c in commands if isinstance(c, Spawn)]
        after_spawn = state.clone()
        expected = dict(state.units)
        budget = state.resources[team]
        for command in spawns:
            if command.x is None:
                self.assertIsNone(command.y)
                p = state.bases[team]
            else:
                p = command.x, command.y
                building = state.building_at(*p)
                self.assertIsNotNone(building)
                self.assertEqual((building.btype, building.owner), ("HOSPITAL", team))
                self.assertNotEqual(p, state.bases[team])
            budget -= command.count * unit_cost(state, team, command.kind)
            self.assertGreaterEqual(budget, 0, "생산 자원 초과")
            key = (*p, team, command.kind)
            expected[key] = expected.get(key, 0) + command.count
        step_spawn(after_spawn, {"Y": spawns if team == "Y" else [],
                                 "K": spawns if team == "K" else []})
        self.assertEqual(after_spawn.units, expected)
        self.assertEqual(after_spawn.resources[team], budget)

        available = defaultdict(int, after_spawn.units)
        movements = [c for c in commands if isinstance(c, (Move, Move2, Tele))]
        teleports = [c for c in movements if isinstance(c, Tele)]
        self.assertLessEqual(len(teleports), state.config["tele"]["per_turn"])
        for command in movements:
            key = command.x, command.y, team, command.kind
            self.assertLessEqual(command.count, available[key], "같은 병력을 중복 이동")
            available[key] -= command.count
            if isinstance(command, Tele):
                self.assertLessEqual(command.count, state.config["tele"]["max_units"])
                self.assertNotEqual((command.x, command.y), (command.tx, command.ty))
                for p in [(command.x, command.y), (command.tx, command.ty)]:
                    building = state.building_at(*p)
                    self.assertIsNotNone(building)
                    self.assertEqual((building.btype, building.owner), ("STATION", team))
                self.assertGreaterEqual(sum(b.btype == "STATION" and b.owner == team
                                            for b in state.buildings.values()), 2)
            else:
                p = command.x, command.y
                dirs = (command.dir1, command.dir2) if isinstance(command, Move2) else (command.direction,)
                for direction in dirs:
                    dx, dy = DIRECTIONS[direction]
                    p = p[0] + dx, p[1] + dy
                    self.assertTrue(state.is_passable(*p))
        before_count = sum(after_spawn.units.values())
        step_move(after_spawn, {"Y": movements if team == "Y" else [],
                                "K": movements if team == "K" else []})
        self.assertEqual(sum(after_spawn.units.values()), before_count)
        return commands

    def test_base_spawns_move_and_capture_in_same_turn(self):
        state = make_state(
            [Building(0, 1, 7, "HALL", 2), Building(1, 0, 6, "ENG", 2)],
            resources={"Y": 10, "K": 10},
        )
        commands = self.assert_commands_executable(state, "Y", decide(make_bot(state), state, 1))
        spawns = [c for c in commands if isinstance(c, Spawn)]
        self.assertEqual(sum(c.count for c in spawns if c.kind == "F"), 2)
        self.assertTrue(all(c.x is None and c.y is None for c in spawns))
        result, _ = run_turn(state, commands, [])
        self.assertEqual([b.owner for b in result.sorted_buildings()], ["Y", "Y"])
        self.assertEqual(result.get_unit(0, 7, "Y", "F"), 0)
        self.assertEqual(result.resources["Y"], 6)

    def test_owned_hospital_is_used_as_forward_spawn_source(self):
        state = make_state(
            [Building(0, 8, 7, "HOSPITAL", 2, "Y", 2), Building(1, 9, 7, "ENG", 2)],
            resources={"Y": 10, "K": 0},
        )
        commands = self.assert_commands_executable(state, "Y", decide(make_bot(state), state, 10))
        self.assertTrue(any(isinstance(c, Spawn) and (c.x, c.y) == (8, 7) for c in commands))
        result, _ = run_turn(state, commands, [])
        self.assertEqual(result.buildings[1].owner, "Y")
        self.assertEqual(result.get_unit(9, 7, "Y", "F"), 1)

    def test_flag_escapes_every_possible_adjacent_enemy_move(self):
        state = make_state(
            [Building(0, 8, 7, "ENG", 2)],
            {(6, 7, "Y", "F"): 1, (7, 7, "K", "W"): 1},
        )
        commands = self.assert_commands_executable(state, "Y", decide(make_bot(state), state))
        for direction in [None, *DIRECTIONS]:
            with self.subTest(direction=direction):
                enemy = [] if direction is None else [Move(7, 7, "W", 1, direction)]
                result, _ = run_turn(state, commands, enemy)
                flags = sum(n for (*_, team, kind), n in result.units.items() if (team, kind) == ("Y", "F"))
                self.assertEqual(flags, 1)

    def test_leaving_owned_building_prevents_combat_neutralization(self):
        state = make_state(
            [Building(0, 6, 7, "ENG", 2, "Y", 2)],
            {(6, 7, "Y", "F"): 1, (7, 7, "K", "W"): 1},
        )
        commands = self.assert_commands_executable(state, "Y", decide(make_bot(state), state))
        result, _ = run_turn(state, commands, [Move(7, 7, "W", 1, "L")])
        self.assertEqual(result.buildings[0].owner, "Y")
        self.assertEqual(result.get_unit(6, 7, "Y", "F"), 0)
        self.assertEqual(sum(n for (*_, t, k), n in result.units.items() if (t, k) == ("Y", "F")), 1)

    def test_escort_wins_combat_and_captures_enemy_building_immediately(self):
        state = make_state(
            [Building(0, 6, 7, "ENG", 2, "K", 2)],
            {(5, 7, "Y", "F"): 1, (5, 7, "Y", "W"): 3,
             (6, 7, "K", "F"): 1, (6, 7, "K", "W"): 1},
        )
        commands = self.assert_commands_executable(state, "Y", decide(make_bot(state), state))
        result, _ = run_turn(state, commands, [])
        self.assertEqual(result.buildings[0].owner, "Y")
        self.assertEqual(result.get_unit(6, 7, "Y", "F"), 1)
        self.assertEqual(result.get_unit(6, 7, "K", "F"), 0)
        self.assertEqual(result.get_unit(6, 7, "Y", "W"), 2)

    def test_last_turn_one_step_enemy_target_can_be_neutralized(self):
        state = make_state(
            [Building(0, 6, 7, "ENG", 2, "K", 2)],
            {(5, 7, "Y", "F"): 1},
        )
        state.turn = 159
        commands = self.assert_commands_executable(state, "Y", decide(make_bot(state), state, 160))
        result, _ = run_turn(state, commands, [])
        self.assertEqual(result.buildings[0].owner, "N")

    def test_new_flag_capture_is_included_in_reserve(self):
        positions = [(4, 2), (6, 2), (8, 2), (4, 12), (6, 12), (8, 12)]
        buildings = [Building(i, *p, "WATCH", 4) for i, p in enumerate(positions)]
        buildings.append(Building(6, 1, 7, "ENG", 2))
        state = make_state(buildings, {(*p, "Y", "F"): 1 for p in positions},
                           {"Y": 10, "K": 0})
        commands = self.assert_commands_executable(state, "Y", decide(make_bot(state), state))
        self.assertTrue(any(isinstance(c, Spawn) and c.kind == "F" for c in commands))
        result, _ = run_turn(state, commands, [])
        self.assertTrue(all(b.owner == "Y" for b in result.buildings.values()))

    def test_hidden_score_uses_revealed_rotational_pair(self):
        state = make_state([Building(0, 6, 3, "WATCH", 4), Building(1, 8, 11, "WATCH", 4)])
        state.revealed["Y"].add(1)
        bot = make_bot(state)
        decide(bot, state)
        self.assertEqual(bot.buildings[(6, 3)]["score"], -1)
        self.assertEqual(bot.score(bot.buildings[(6, 3)]), 4)
        self.assertEqual(bot.score(bot.buildings[(8, 11)]), 4)

    def test_unrevealed_actual_scores_do_not_leak_into_decisions(self):
        first = make_state([Building(0, 6, 3, "WATCH", 2), Building(1, 8, 11, "WATCH", 2)],
                           resources={"Y": 10, "K": 0})
        second = first.clone()
        for b in second.buildings.values():
            b.score = 4
        self.assertEqual(decide(make_bot(first), first), decide(make_bot(second), second))

    def test_teleport_is_capped_and_residual_units_move_without_duplication(self):
        state = make_state(
            [Building(0, 1, 7, "STATION", 2, "Y", 2),
             Building(1, 10, 7, "STATION", 2, "Y", 2),
             Building(2, 11, 7, "ENG", 2, "K", 2)],
            {(1, 7, "Y", "W"): 12, (11, 7, "K", "F"): 1, (11, 7, "K", "W"): 5},
        )
        commands = self.assert_commands_executable(state, "Y", decide(make_bot(state), state))
        teleports = [c for c in commands if isinstance(c, Tele)]
        self.assertEqual(len(teleports), 1)
        self.assertEqual(teleports[0].count, 5)
        result, _ = run_turn(state, commands, [])
        self.assertEqual(result.get_unit(10, 7, "Y", "W"), 5)
        self.assertEqual(sum(n for (*_, t, k), n in result.units.items() if (t, k) == ("Y", "W")), 12)

    def test_generated_maps_keep_every_spawn_and_move_within_available_pool(self):
        for seed in [3, 29, 101]:
            state = to_state(generate(seed, load_config()))
            bots = {team: make_bot(state, team) for team in ["Y", "K"]}
            for turn in range(1, 26):
                with self.subTest(seed=seed, turn=turn):
                    actions = {team: self.assert_commands_executable(
                        state, team, decide(bots[team], state, turn)) for team in bots}
                    state, result = run_turn(state, actions["Y"], actions["K"])
                    if result:
                        break


if __name__ == "__main__":
    unittest.main()
