from __future__ import annotations

import hashlib
import json
import re


def object_schema(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


STR = {"type": "string"}
STRINGS = {"type": "array", "items": STR}
STEP_SCHEMA = object_schema({
    "title": STR, "instructions": STR,
    "checks": {"type": "array", "items": {"type": "array", "items": STR, "minItems": 1}, "minItems": 1},
})
PLAN_SCHEMA = object_schema({"goal": STR, "scope": STR,
                             "steps": {"type": "array", "items": STEP_SCHEMA, "minItems": 1, "maxItems": 12}})
RESULT_SCHEMA = object_schema({
    "status": {"type": "string", "enum": ["step_done", "continue", "retry", "needs_input", "needs_approval", "blocked"]},
    "summary": STR, "evidence": STRINGS, "next_prompt": STR, "question": STR,
})
SUGGEST_SCHEMA = object_schema({"prompt": STR, "reason": STR})


def parse_object(text):
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object")
    return result


def validate_plan(plan):
    if not isinstance(plan.get("goal"), str) or not plan["goal"].strip():
        raise ValueError("계획 목표가 없습니다.")
    if not isinstance(plan.get("scope"), str) or not plan["scope"].strip():
        raise ValueError("계획의 허용 범위가 없습니다.")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 12:
        raise ValueError("계획은 1–12단계여야 합니다.")
    for step in steps:
        if not all(isinstance(step.get(k), str) and step[k].strip() for k in ("title", "instructions")):
            raise ValueError("단계 설명이 없습니다.")
        if not isinstance(step.get("checks"), list) or not step["checks"]:
            raise ValueError("단계마다 완료 검증 명령이 필요합니다.")
        for command in step["checks"]:
            if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
                raise ValueError("검증 명령은 문자열 배열이어야 합니다.")
            if len(command) > 80 or sum(map(len, command)) > 8000:
                raise ValueError("검증 명령이 너무 깁니다.")
    return plan


def plan_hash(plan):
    contract = {key: plan[key] for key in ("goal", "scope", "steps")}
    return hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def routine_question(text):
    """Resolve only a workflow-continuation question inside an approved plan.

    This is never used for native permission, command, or connector approvals.
    """
    if not text or len(text) > 1200:
        return False
    if re.search(r"permission|approv|install|upload|download|delete|commit|push|payment|credential|"
                 r"권한|승인|설치|업로드|다운로드|삭제|커밋|결제|가설|제외|연구.?설계", text, re.I):
        return False
    return bool(re.search(r"진행할까요|계속할까요|이어.?갈까요|계속.?진행|"
                          r"(?:shall|should|can) (?:I|we) (?:continue|proceed)|continue with (?:the )?(?:plan|next step)",
                          text, re.I))


def sensitive_question(text):
    return bool(re.search(r"permission|approv|grant access|install|upload|download|delete|commit|push|payment|"
                          r"권한|승인|설치|업로드|다운로드|삭제|커밋|결제", text, re.I))


def step_prompt(plan, index):
    step = plan["steps"][index]
    return (f"승인된 계획의 {index + 1}/{len(plan['steps'])}단계를 수행하세요.\n"
            f"목표: {plan['goal']}\n허용 범위: {plan['scope']}\n"
            f"현재 단계: {step['title']}\n지시: {step['instructions']}\n"
            "이미 승인된 현재 단계의 일상적인 구현·검증·다음 작업을 다시 물을 필요는 없습니다. "
            "기존 AGENTS.md, 원본 보호, 실제 권한 및 사용자 중단을 따르세요. "
            "새 권한이나 계획 밖 결정을 임의로 승인하지 마세요. "
            "현재 단계가 완료됐으면 step_done, 계속 작업할 수 있으면 continue, "
            "진짜 사용자 정보·권한이 필요하면 needs_input 또는 needs_approval, "
            "해결할 수 없는 문제가 있으면 blocked로 반환하세요. "
            "evidence에는 실제 결과와 검증 근거를 적으세요.")
