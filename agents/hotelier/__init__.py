"""Агент-отельер и имитатор (core §7.2): вопрос → свободный ответ → поля вопроса → core/hotelier."""

from agents.hotelier.agent import AgentAnswer, answer_from_text
from agents.hotelier.parse_answer import ParsedAnswer, parse_answer
from agents.hotelier.questions import enrich_question, placement_question
from agents.hotelier.simulator import SIMULATED_NOTE, simulate_answer, simulate_placement

__all__ = [
    "SIMULATED_NOTE",
    "AgentAnswer",
    "ParsedAnswer",
    "answer_from_text",
    "enrich_question",
    "parse_answer",
    "placement_question",
    "simulate_answer",
    "simulate_placement",
]
