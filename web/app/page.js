"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Image from "next/image";
import Icon from "./components/Icon";

const SUGGESTIONS = [
  "바이퍼의 평균 KDA를 알려줘",
  "페이커의 사용한 챔피언을 알려줘",
  "한화생명의 우승 전략을 알려줘",
  "승률이 가장 높은 팀을 알려줘",
];

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";

//베타테스트용
const GRAPH_NODE_THEMES = {

    // ===== Team 계열 (Blue) =====
    Team: { base: "#B9C8F3", border: "#7187CB", glow: "#E8EDFC", text: "#25324A" },
    Player: { base: "#A9D1F5", border: "#6198C8", glow: "#E5F3FE", text: "#25324A" },
    Role: { base: "#C7BDF2", border: "#8877C7", glow: "#EEEAFB", text: "#302A49" },
    Coach: { base: "#D3C4EC", border: "#9580BB", glow: "#F1ECF8", text: "#342D46" },

    Position: {
        base: "#A8E0E8",
        border: "#5AA3AE",
        glow: "#E2F6F8",
        text: "#1f2937"
    },


    // ===== Game 계열 (Green) =====
    Champion: {
        base: "#94E2DF",
        border: "#43AAA6",
        glow: "#DDF7F5",
        text: "#1f2937"
    },

    Entity: {
        base: "#A8DDC8",
        border: "#5B9F84",
        glow: "#E3F5ED",
        text: "#1f2937"
    },

    Objective: {
        base: "#B9E0AC",
        border: "#72A660",
        glow: "#EAF6E5",
        text: "#1f2937"
    },

    Strategy: {
        base: "#D4E5A5",
        border: "#98AD59",
        glow: "#F1F7DF",
        text: "#1f2937"
    },

    StrategyEvidence: {
        base: "#D4E5A5",
        border: "#98AD59",
        glow: "#F1F7DF",
        text: "#1f2937"
    },

    InferredStrategy: {
        base: "#C9D99A",
        border: "#7F9740",
        glow: "#EEF5D6",
        text: "#1f2937"
    },

    // 이전 응답 캐시와의 표시 호환성만 유지한다.
    StrategyInsight: {
        base: "#C9D99A",
        border: "#7F9740",
        glow: "#EEF5D6",
        text: "#1f2937"
    },

    Structure: {
        base: "#F0DDA4",
        border: "#B69A4F",
        glow: "#FBF4DF",
        text: "#1f2937"
    },

    Spell: {
        base: "#F3CEA0",
        border: "#BE8950",
        glow: "#FCEDDD",
        text: "#1f2937"
    },


    // ===== 경기 계열 (Red/Warm) =====
    Game: {
        base: "#F2BDA7",
        border: "#BD7760",
        glow: "#FBE7DF",
        text: "#1f2937"
    },

    Match: {
        base: "#F1AEA9",
        border: "#BD6964",
        glow: "#FBE1DF",
        text: "#1f2937"
    },

    Tournament: {
        base: "#E9B2CB",
        border: "#B86D90",
        glow: "#F8E3EC",
        text: "#1f2937"
    },

    League: {
        base: "#DDB7DE",
        border: "#A270A4",
        glow: "#F4E5F4",
        text: "#1f2937"
    },

    Patch: {
        base: "#C8B9E4",
        border: "#8973B1",
        glow: "#EEE8F7",
        text: "#1f2937"
    },

    Day: {
        base: "#B8C1DB",
        border: "#74809F",
        glow: "#E9ECF4",
        text: "#1f2937"
    },
};

const GRAPH_NODE_LABELS = {
  Team: "TEAM",
  Player: "PLAYER",
  Champion: "CHAMPION",
  Tournament: "TOURNAMENT",
  League: "LEAGUE",
  Day: "DAY",
  Match: "MATCH",
  Game: "GAME",
  StrategyEvidence: "전략 근거",
  InferredStrategy: "추론 전략",
  StrategyInsight: "추론 전략",
};

// 위 표에 없는 라벨(Group/Item/Stage/Venue/Node 등)에 쓰는 중립 파스텔.
// vis-network 기본 팔레트(원색)가 새지 않도록 모든 노드에 이 색을 폴백으로 강제한다.
const DEFAULT_NODE_THEME = {
    base: "#D3D9E0",
    border: "#8B96A3",
    glow: "#F0F2F5",
    text: "#1f2937"
};

const GRAPH_NODE_GROUPS = Object.fromEntries(
  Object.entries(GRAPH_NODE_THEMES).map(([group, theme]) => [
    group,
    {
      color: {
        background: theme.base,
        border: theme.border,
        highlight: { background: "#ffbf00", border: "#cc9900" },
        hover: { background: theme.base, border: theme.glow },
      },
    },
  ])
);

const DEFAULT_NODE_COLOR = {
  background: DEFAULT_NODE_THEME.base,
  border: DEFAULT_NODE_THEME.border,
  highlight: { background: "#ffbf00", border: "#cc9900" },
  hover: { background: DEFAULT_NODE_THEME.base, border: DEFAULT_NODE_THEME.glow },
};

// 라벨 → 파스텔 색. 미지정 라벨도 원색 대신 중립 파스텔을 쓰게 한다.
function nodeColorFor(label) {
  return (GRAPH_NODE_GROUPS[label] && GRAPH_NODE_GROUPS[label].color) || DEFAULT_NODE_COLOR;
}

function hexToRgb(hex) {
  const value = String(hex || "").replace("#", "");
  if (value.length !== 6) return { r: 56, g: 189, b: 248 };
  return {
    r: parseInt(value.slice(0, 2), 16),
    g: parseInt(value.slice(2, 4), 16),
    b: parseInt(value.slice(4, 6), 16),
  };
}

function rgbToHex({ r, g, b }) {
  return `#${[r, g, b].map((v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0")).join("")}`;
}

function mixColor(from, to, amount) {
  const a = hexToRgb(from);
  const b = hexToRgb(to);
  return rgbToHex({
    r: a.r + (b.r - a.r) * amount,
    g: a.g + (b.g - a.g) * amount,
    b: a.b + (b.b - a.b) * amount,
  });
}

function withAlpha(hex, alpha) {
  const { r, g, b } = hexToRgb(hex);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function fitNodeLabel(ctx, label, maxWidth, maxSize, minSize) {
  const raw = String(label || "").trim() || "-";

  for (let size = maxSize; size >= minSize; size -= 1) {
    ctx.font = `800 ${size}px Pretendard, Inter, Noto Sans KR, Arial, sans-serif`;
    if (ctx.measureText(raw).width <= maxWidth) return { text: raw, size };
  }

  ctx.font = `800 ${minSize}px Pretendard, Inter, Noto Sans KR, Arial, sans-serif`;
  let trimmed = raw;
  while (trimmed.length > 1 && ctx.measureText(`${trimmed}...`).width > maxWidth) {
    trimmed = trimmed.slice(0, -1);
  }
  return { text: trimmed.length < raw.length ? `${trimmed}...` : raw, size: minSize };
}

function createSoftNodeRenderer(nodeInfoById, radius) {
  return ({ ctx, id, x, y, state, label }) => ({
    drawNode: () => {
      const info = nodeInfoById.get(String(id)) || {};
      const group = info.group || "";
      const displayRadius = radius + (info.degree ? Math.min(8, info.degree * 1.2) : 0) + (group === "Team" ? 4 : 0);
      const theme = GRAPH_NODE_THEMES[group] || DEFAULT_NODE_THEME;
      const selected = Boolean(state.selected);
      const hover = Boolean(state.hover);
      const active = selected || hover;
      const haloRadius = displayRadius + (selected ? 10 : hover ? 8 : 5);
      const nodeBase = selected ? "#ffbf00" : theme.base;
      const center = mixColor(nodeBase, "#ffffff", 0.18);
      const edge = mixColor(nodeBase, "#6b7280", 0.06);
      const rim = selected ? "#cc9900" : theme.border;

      ctx.save();
      ctx.beginPath();
      ctx.arc(x, y, haloRadius, 0, Math.PI * 2);
      ctx.fillStyle = withAlpha(theme.glow, selected ? 0.18 : hover ? 0.13 : 0.08);
      ctx.shadowColor = withAlpha(theme.border, selected ? 0.2 : 0.12);
      ctx.shadowBlur = selected ? 14 : 8;
      ctx.fill();
      ctx.restore();

      ctx.save();
      ctx.beginPath();
      ctx.arc(x, y, displayRadius, 0, Math.PI * 2);
      ctx.shadowColor = "rgba(31, 41, 55, 0.2)";
      ctx.shadowBlur = 11;
      ctx.shadowOffsetY = 6;
      const fill = ctx.createRadialGradient(x, y - displayRadius * 0.18, displayRadius * 0.14, x, y, displayRadius);
      fill.addColorStop(0, center);
      fill.addColorStop(0.72, nodeBase);
      fill.addColorStop(1, edge);
      ctx.fillStyle = fill;
      ctx.fill();
      ctx.restore();

      ctx.save();
      ctx.beginPath();
      ctx.arc(x, y, displayRadius, 0, Math.PI * 2);
      ctx.strokeStyle = withAlpha(rim, active ? 0.85 : 0.5);
      ctx.lineWidth = selected ? 2.1 : 1.4;
      ctx.shadowColor = active ? withAlpha(theme.glow, 0.35) : "transparent";
      ctx.shadowBlur = active ? 8 : 0;
      ctx.stroke();
      ctx.restore();

      if (active) {
        ctx.save();
        ctx.beginPath();
        ctx.arc(x, y, displayRadius + 5, 0, Math.PI * 2);
        ctx.strokeStyle = withAlpha(theme.border, selected ? 0.35 : 0.22);
        ctx.lineWidth = selected ? 1.8 : 1.2;
        ctx.stroke();
        ctx.restore();
      }

      ctx.save();
      ctx.beginPath();
      ctx.arc(x, y, displayRadius - 4, 0, Math.PI * 2);
      ctx.strokeStyle = "rgba(255, 255, 255, 0.46)";
      ctx.lineWidth = 1.2;
      ctx.stroke();
      ctx.restore();

      const fitted = fitNodeLabel(ctx, label, displayRadius * 1.45, displayRadius > 30 ? 14 : 12, 9);
      const typeLabel = GRAPH_NODE_LABELS[group] || String(group || "NODE").toUpperCase();
      ctx.save();
      ctx.font = `800 ${fitted.size}px Pretendard, Inter, Noto Sans KR, Arial, sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.lineWidth = 3;
      ctx.strokeStyle = "rgba(255, 255, 255, 0.5)";
      ctx.strokeText(fitted.text, x, y - 3);
      ctx.fillStyle = theme.text;
      ctx.fillText(fitted.text, x, y - 3);

      ctx.font = `800 ${displayRadius > 30 ? 9 : 8}px Pretendard, Inter, Noto Sans KR, Arial, sans-serif`;
      ctx.letterSpacing = "0px";
      ctx.fillStyle = withAlpha(theme.text, 0.68);
      ctx.fillText(typeLabel, x, y + displayRadius * 0.38);
      ctx.restore();
    },
    nodeDimensions: { width: (radius + 14) * 2, height: (radius + 14) * 2 },
  });
}

function buildGraphNodeInfo(nodes, links) {
  const degreeById = new Map();
  for (const link of links || []) {
    degreeById.set(String(link.source), (degreeById.get(String(link.source)) || 0) + 1);
    degreeById.set(String(link.target), (degreeById.get(String(link.target)) || 0) + 1);
  }

  return new Map(
    (nodes || []).map((node) => [
      String(node.id),
      {
        group: node.label,
        degree: degreeById.get(String(node.id)) || 0,
        importance: Number(node.importance) || 0,
        graphRole: node.graph_role || "",
      },
    ])
  );
}

function graphNodeSize(node, info = {}, compact = false) {
  const importance = Math.max(1, Math.min(10, Number(node.importance) || 5));
  const boost = Math.max(-1.5, Math.min(2.5, (importance - 5) * 0.45));
  const scale = compact ? 0.82 : 1;

  if (node.graph_role === "opponent_team") return 10 * scale;
  if (node.graph_role === "focus_team") return (18 + Math.max(0, boost)) * scale;

  const baseByLabel = {
    Match: 23,
    InferredStrategy: 18,
    StrategyInsight: 18,
    StrategyEvidence: 9.5,
    Team: 16,
    Player: 13,
    Champion: 12,
    Tournament: 12.5,
    League: 11.5,
    Game: 14,
  };
  const base = baseByLabel[node.label] || 12.5;
  const labelBoost = node.label === "StrategyEvidence" ? boost * 0.25 : boost;
  return Math.max(8, (base + labelBoost) * scale);
}

function graphNodeMass(node, info = {}) {
  if (node.graph_role === "opponent_team") return 0.8;
  if (node.graph_role === "focus_team") return 2.2;
  if (node.label === "Match") return 2;
  if (node.label === "InferredStrategy") return 1.45;
  return 1;
}

function graphEdgeLength(link, nodeInfoById, compact = false) {
  const source = nodeInfoById.get(String(link.source)) || {};
  const target = nodeInfoById.get(String(link.target)) || {};
  const opponentEdge = source.graphRole === "opponent_team" || target.graphRole === "opponent_team";
  const baseByType = {
    PLAYED_MATCH: opponentEdge ? 105 : 150,
    WON_MATCH: 150,
    HAS_INFERRED_STRATEGY: 135,
    HAS_STRATEGY_EVIDENCE: 108,
    PART_OF_TOURNAMENT: 118,
    BELONGS_TO_LEAGUE: 96,
  };
  const length = baseByType[link.type] || 125;
  return compact ? length * 0.8 : length;
}

function graphEdgeWidth(link) {
  const weight = Math.max(1, Math.min(5, Number(link.weight) || 1));
  return Math.min(2.1, 0.9 + (weight - 1) * 0.3);
}

function graphEdgeColor(link, nodeInfoById) {
  const group = nodeInfoById.get(String(link.source))?.group || nodeInfoById.get(String(link.target))?.group;
  const theme = GRAPH_NODE_THEMES[group] || DEFAULT_NODE_THEME;
  return {
    color: withAlpha(theme.border, 0.62),
    highlight: theme.border,
    hover: theme.border,
    opacity: 0.8,
  };
}

// 관계 표준명 → 한국어 라벨 (엣지 hover/선택 시 표시)
const REL_LABEL_KO = {
  MEMBER_OF: "소속", PLAYS_FOR: "소속", COACHES: "감독·코치", PLAYS_ROLE: "포지션",
  PICKED: "챔피언 사용", PICKED_CHAMPION: "챔피언 사용", PICKED_IN: "픽된 게임",
  USED_STRATEGY: "전술·운영", DIRECTED: "감독 지시", MADE_CALL: "선수 판단",
  WON: "우승", WON_GAME: "게임 승리", WON_MATCH: "매치 승리",
  BEAT: "상대 전적", COMPETES_WITH: "경쟁 상대",
  PLAYED_IN: "참가·출전", PLAYED_GAME: "게임 출전", PLAYED_MATCH: "매치 출전",
  TEAM_GAME_STAT: "게임 스탯", PART_OF_MATCH: "소속 매치", PART_OF_TOURNAMENT: "소속 대회",
  BELONGS_TO_LEAGUE: "소속 리그", PLAYED_ON: "경기일", CAPTURED: "오브젝트 획득",
  HAS_STRATEGY_EVIDENCE: "전략 근거",
  HAS_INFERRED_STRATEGY: "경기별 추론 전략",
  USES_INFERRED_STRATEGY: "추론 전략",
  SUPPORTED_BY_MATCH: "대표 근거 경기",
};
function koRel(type) {
  if (!type) return "";
  return REL_LABEL_KO[type] || String(type);
}

// 상세 요약 키워드 줄에 쓰는 관계별 아이콘
const REL_EMOJI = {
  "소속 선수": "", "감독·코치": "", "포지션": "", "사용 챔피언": "",
  "전술·운영": "", "감독 지시": "", "선수 판단": "", "우승": "",
  "승리한 게임": "", "승리한 매치": "", "상대 전적": "", "경쟁 상대": "",
  "참가·출전": "", "출전 게임": "", "출전 매치": "", "게임 스탯": "",
  "소속 매치": "", "소속 대회": "", "소속 리그": "", "경기일": "",
  "픽된 게임": "", "오브젝트 획득": "",
};
function relEmoji(relKo) {
  return REL_EMOJI[relKo] || "";
}

function formatGraphFact(fact) {
  if (!fact || typeof fact !== "object") return String(fact || "");
  return `${fact.source || "-"} — ${koRel(fact.relation)} → ${fact.target || "-"}`;
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function EvidenceSection({ title, count, tone = "", open, onToggle, children }) {
  return (
    <section className={`evidence-section ${tone}${open ? " open" : ""}`}>
      <button
        type="button"
        className="evidence-section-toggle"
        aria-expanded={open}
        onClick={onToggle}
      >
        <span className="evidence-section-chevron" aria-hidden="true">›</span>
        <span className="evidence-section-title">{title}</span>
        {count !== undefined && count !== null && (
          <span className="evidence-section-count">{count}</span>
        )}
        <span className="evidence-section-state">{open ? "접기" : "펼치기"}</span>
      </button>
      {open && <div className="evidence-section-body">{children}</div>}
    </section>
  );
}

// 노드에 hover/선택할 때만 그 노드에 연결된 엣지의 (한글) 관계명을 표시한다.
// 평소엔 라벨을 비워 그래프를 깔끔하게 유지 → 난잡함 없이 필요할 때만 관계가 읽힌다.
function attachEdgeLabelReveal(network, edges, edgeLabelById) {
  let selected = null;
  let hovered = null;
  const apply = () => {
    const showIds = new Set();
    for (const nodeId of [hovered, selected]) {
      if (nodeId == null) continue;
      for (const eid of network.getConnectedEdges(nodeId)) showIds.add(eid);
    }
    const updates = [];
    edges.forEach((e) => {
      const desired = showIds.has(e.id) ? (edgeLabelById.get(e.id) || "") : "";
      if ((e.label || "") !== desired) updates.push({ id: e.id, label: desired });
    });
    if (updates.length) edges.update(updates);
  };
  network.on("hoverNode", (p) => { hovered = p.node; apply(); });
  network.on("blurNode", () => { hovered = null; apply(); });
  network.on("selectNode", (p) => { selected = (p.nodes && p.nodes[0]) != null ? p.nodes[0] : null; apply(); });
  network.on("deselectNode", () => { selected = null; apply(); });
}

const CITATION_STOPWORDS = new Set([
  "그리고", "그러나", "또한", "이는", "이런", "저런", "해당", "대한", "통해", "기반",
  "구체적인", "확인되지", "어렵습니다", "있습니다", "합니다", "됩니다", "입니다",
]);

function tokenizeEvidenceText(text) {
  return String(text || "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}.]+/gu, " ")
    .split(/\s+/)
    .map((token) => token.trim())
    .filter((token) => token.length >= 2 && !CITATION_STOPWORDS.has(token));
}

function splitAnswerSentences(text) {
  // 줄 단위로 먼저 나눈다(빈 줄 무시). 목록 항목("1. …", "- …")은 통째로 한 조각으로 유지해
  // 목록 번호의 마침표가 문장끝으로 오인되어 번호와 내용이 따로 떨어지는 걸 막는다.
  const lines = String(text || "")
    .split(/\n+/)
    .map((l) => l.trim())
    .filter(Boolean);

  const parts = [];
  for (const line of lines) {
    if (/^(\d+[.)]|[-•*])\s+/.test(line)) {
      parts.push(line); // 목록 항목은 분해하지 않음
      continue;
    }
    let start = 0;
    for (let i = 0; i < line.length; i += 1) {
      const char = line[i];
      if (!".!?。！？".includes(char)) continue;
      // 6.12, 5.3처럼 양옆이 숫자인 점은 문장부호가 아니라 소수점이다.
      if (char === "." && /\d/.test(line[i - 1] || "") && /\d/.test(line[i + 1] || "")) continue;
      let end = i + 1;
      while (end < line.length && ".!?。！？".includes(line[end])) end += 1;
      const sentence = line.slice(start, end).trim();
      if (sentence) parts.push(sentence);
      start = end;
      i = end - 1;
    }
    const remainder = line.slice(start).trim();
    if (remainder) parts.push(remainder);
  }
  return parts.length ? parts : [String(text || "")];
}

function pickSentenceEvidence(sentence, evidenceItems) {
  const sentenceTokens = new Set(tokenizeEvidenceText(sentence));
  if (sentenceTokens.size === 0) return [];

  return evidenceItems
    .map((item) => {
      let score = 0;
      for (const token of sentenceTokens) {
        if (item.tokenSet.has(token)) score += token.length >= 4 ? 2 : 1;
      }
      return { item, score };
    })
    .filter(({ score }) => score > 0)
    .sort((a, b) => b.score - a.score || a.item.number - b.item.number)
    .slice(0, 2)
    .map(({ item }) => item);
}

// 핵심 분석 대상 칩 값: 질문 문장 대신 "명사형 엔티티"를 골라낸다.
// 우선순위: 인식된 선수/팀 → 그래프의 Team/Player 노드명 → 명사형 키워드 → 질문
function pickCoreTarget(data, question) {
  const uniq = (arr) => [...new Set(arr.filter(Boolean))];

  const ents = uniq([
    ...((data.rdb_entities && data.rdb_entities.players) || []),
    ...((data.rdb_entities && data.rdb_entities.teams) || []),
  ]);
  if (ents.length) return ents.slice(0, 3).join(", ");

  const nodes = (data.graph && data.graph.nodes) || [];
  const named = uniq(
    nodes
      .filter((n) => n.label === "Team" || n.label === "Player")
      .map((n) => n.name)
  );
  if (named.length) return named.slice(0, 3).join(", ");

  // 키워드 폴백: 질문 문장(공백 포함 긴 구절)은 제외하고 짧은 명사 토큰만
  const kws = Array.isArray(data.graph_keyword) ? data.graph_keyword : [];
  const nouns = uniq(
    kws.filter((k) => typeof k === "string" && !k.includes(" ") && k.length <= 8)
  );
  if (nouns.length) return nouns.slice(0, 3).join(", ");

  return question;
}

// 이전 질문(응답)에 등장한 엔티티로 관련 후속 추천 질문을 만든다.
function buildFollowUpSuggestions(data) {
  const nodes = (data.graph && data.graph.nodes) || [];
  const uniq = (arr) => [...new Set(arr.filter(Boolean))];
  const byLabel = (lbl) => uniq(nodes.filter((n) => n.label === lbl).map((n) => n.name));
  const teams = byLabel("Team");
  const players = byLabel("Player");
  const tours = byLabel("Tournament");

  const out = [];
  if (players[0]) out.push(`${players[0]}가 사용한 챔피언을 알려줘`);
  if (teams[0]) out.push(`${teams[0]}의 주요 선수를 알려줘`);
  if (teams[0] && teams[1]) out.push(`${teams[0]}와 ${teams[1]}의 관계를 알려줘`);
  else if (teams[0]) out.push(`${teams[0]}가 우승한 대회를 알려줘`);
  if (tours[0]) out.push(`${tours[0]}에 대해 알려줘`);
  if (players[1]) out.push(`${players[1]}의 소속팀을 알려줘`);

  return uniq(out).slice(0, 4);
}

// LLM이 가끔 마크다운(**굵게**, #제목, - 목록 등)으로 답을 뱉는데
// 프론트는 순수 텍스트로 렌더링하므로 기호가 그대로 노출된다 → 표시 전에 벗겨낸다.
function stripMarkdown(text) {
  return String(text || "")
    .replace(/```[\s\S]*?```/g, "")        // 코드블록
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1") // 이미지
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")  // [텍스트](링크) → 텍스트
    .replace(/\*\*([^*]+)\*\*/g, "$1")       // **굵게**
    .replace(/__([^_]+)__/g, "$1")           // __굵게__
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1$2") // *기울임*
    .replace(/`([^`]+)`/g, "$1")             // `코드`
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")       // # 제목
    .replace(/^\s*>\s?/gm, "")               // > 인용
    .replace(/^\s*[-*+]\s+/gm, "")            // - 목록 기호
    .replace(/[*_`#]/g, "")                  // 남은 잔여 기호 제거
    .replace(/[ \t]{2,}/g, " ")
    .replace(/^[ \t]+/gm, "")                // 각 줄 앞 들여쓰기 제거
    .replace(/[ \t]+$/gm, "")                // 각 줄 끝 공백 제거
    .replace(/\n{3,}/g, "\n\n")              // 3줄 이상 빈 줄 → 최대 1개만 (섹션 구분은 유지, 과한 간격만 제거)
    .trim();
}

export default function Home() {
  const theme = "light";
  const [active, setActive] = useState(false);
  const [question, setQuestion] = useState("");
  const [followUp, setFollowUp] = useState("");
  const [followUpSuggestions, setFollowUpSuggestions] = useState([]);
  const [history, setHistory] = useState([]);

  // 채팅 로그: [{ q, a }]  (a === null 이면 답변 생성 중)
  const [chat, setChat] = useState([]);
  // 오른쪽 패널 탭: summary | evidence
  const [activeTab, setActiveTab] = useState("summary");

  const [apiStatus, setApiStatus] = useState({ text: "API 연결 대기", state: "" });
  const [queryStatus, setQueryStatus] = useState("질문을 입력하면 분석이 시작됩니다.");
  const [analysisState, setAnalysisState] = useState("대기");
  const [coreTarget, setCoreTarget] = useState("-");
  const [period, setPeriod] = useState("전체 기간");

  // answerMode: empty | loading | text | error
  const [answerMode, setAnswerMode] = useState("empty");
  const [answerText, setAnswerText] = useState("아직 질문이 입력되지 않았습니다.");
  const [detailedAnswerText, setDetailedAnswerText] = useState("");
  const [citationPopup, setCitationPopup] = useState(null);

  const [chunks, setChunks] = useState(null); // null | string[]
  const [articleEvidence, setArticleEvidence] = useState(null); // null | evidence[]
  const [rdbFactsList, setRdbFactsList] = useState([]);

  // rdbMode: empty | loading | none | content | error
  const [rdbMode, setRdbMode] = useState("empty");
  const [rdb, setRdb] = useState(null);
  const [rdbScope, setRdbScope] = useState(null);
  const [rdbEntityIds, setRdbEntityIds] = useState({ players: [], teams: [] });
  const [rdbTruncated, setRdbTruncated] = useState(false);
  const [rdbTotalFacts, setRdbTotalFacts] = useState(0);

  const [graphData, setGraphData] = useState(null);
  const [selectedNodeId, setSelectedNodeId] = useState(null);
  const [hoveredNodeId, setHoveredNodeId] = useState(null);

  // 노드 클릭 → 아래 상세 대시보드 (별도 레이아웃)
  const [detailNode, setDetailNode] = useState(null);
  const [detailData, setDetailData] = useState(null);
  const [detailMode, setDetailMode] = useState("empty"); // empty|loading|content|none|error

  const graphRef = useRef(null);
  const networkRef = useRef(null);
  const detailGraphRef = useRef(null);
  const detailNetworkRef = useRef(null);
  const fetchDetailRef = useRef(null);

  const [legendCollapsed, setLegendCollapsed] = useState(true);
  const [suggestionsCollapsed, setSuggestionsCollapsed] = useState(false);
  const [evidenceOpen, setEvidenceOpen] = useState({
    node: false,
    article: false,
    kg: false,
    rdb: false,
  });

  // 테마 적용 + 그래프 글자색 갱신
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    const nodeFontColor = theme === "light" ? "#1f2430" : "#e7e9ee";
    if (networkRef.current) {
      networkRef.current.setOptions({
        nodes: { font: { color: nodeFontColor } },
        edges: {
          font: {
            color: theme === "light" ? "#5f6673" : "#c9cdd6",
            strokeWidth: 4,
            strokeColor: theme === "light" ? "rgba(250, 250, 248, 0.88)" : "rgba(9, 10, 13, 0.82)",
            background: theme === "light" ? "rgba(255, 255, 255, 0.86)" : "rgba(17, 19, 24, 0.86)",
          },
        },
      });
    }
    if (detailNetworkRef.current) {
      detailNetworkRef.current.setOptions({
        nodes: { font: { color: nodeFontColor } },
        edges: {
          font: {
            color: theme === "light" ? "#5f6673" : "#c9cdd6",
            strokeWidth: 4,
            strokeColor: theme === "light" ? "rgba(250, 250, 248, 0.88)" : "rgba(9, 10, 13, 0.82)",
            background: theme === "light" ? "rgba(255, 255, 255, 0.86)" : "rgba(17, 19, 24, 0.86)",
          },
        },
      });
    }
  }, [theme]);

  // 그래프 (재)생성
  useEffect(() => {
    let cancelled = false;
    async function build() {
      const container = graphRef.current;
      if (!container) return;
      if (!graphData || !graphData.nodes || graphData.nodes.length === 0) {
        networkRef.current = null;
        return;
      }
      const vis = await import("vis-network/standalone");
      if (cancelled) return;
      const { Network, DataSet } = vis;
      const nodeInfoById = buildGraphNodeInfo(graphData.nodes, graphData.links);

      const nodes = new DataSet(
        graphData.nodes.map((n) => {
          const info = nodeInfoById.get(String(n.id)) || {};
          return {
            id: n.id,
            label: n.name,
            group: n.label,
            color: nodeColorFor(n.label), // 모든 타입 파스텔 (미지정 라벨도 중립 파스텔)
            // 답변 중요도는 유지하되 크기 변화폭을 압축해 한 노드가 화면을 지배하지 않게 한다.
            size: graphNodeSize(n, info),
            mass: graphNodeMass(n, info),
            title: n.title || `${n.label}: ${n.name}`,
          };
        })
      );
      const edgeLabelById = new Map();
      const edges = new DataSet(
        graphData.links.map((l, i) => {
          const id = `e${i}`;
          edgeLabelById.set(id, koRel(l.type));
          return {
            id,
            from: l.source,
            to: l.target,
            label: "", // 평소엔 숨김. hover/선택 시에만 표시
            arrows: { to: { enabled: true, scaleFactor: 0.75 } },
            color: graphEdgeColor(l, nodeInfoById),
            width: graphEdgeWidth(l),
            length: graphEdgeLength(l, nodeInfoById),
          };
        })
      );

      const options = {
        nodes: {
          shape: "dot",
          size: 15,
          borderWidth: 1.6,
          font: {
            size: 12,
            face: "Pretendard",
            color: theme === "light" ? "#303846" : "#e7e9ee",
            strokeWidth: 3,
            strokeColor: theme === "light" ? "rgba(250, 250, 248, 0.9)" : "rgba(9, 10, 13, 0.86)",
            vadjust: 2,
          },
        },
        edges: {
          font: {
            size: 10,
            align: "middle",
            color: theme === "light" ? "#5f6673" : "#c9cdd6",
            strokeWidth: 3,
            strokeColor: theme === "light" ? "rgba(250, 250, 248, 0.88)" : "rgba(9, 10, 13, 0.82)",
            background: theme === "light" ? "rgba(255, 255, 255, 0.86)" : "rgba(17, 19, 24, 0.86)",
          },
          width: 1,
          selectionWidth: 1.35,
          smooth: { enabled: true, type: "continuous", roundness: 0.22 },
          shadow: false,
        },
        groups: GRAPH_NODE_GROUPS,
        layout: { hierarchical: { enabled: false } },
        // 레벨/좌표는 고정하지 않고 vis-network 물리를 사용하되, 짧은 스프링으로
        // 중앙에 뭉치지 않도록 관계별 길이와 겹침 방지를 강화한다.
        physics: {
          enabled: true,
          solver: "barnesHut",
          stabilization: { enabled: true, iterations: 260, updateInterval: 25, fit: true },
          barnesHut: {
            gravitationalConstant: -9500,
            centralGravity: 0.14,
            springLength: 135,
            springConstant: 0.035,
            damping: 0.4,
            avoidOverlap: 1,
          },
          minVelocity: 0.55,
        },
        interaction: {
          hover: true,
          tooltipDelay: 120,
          hideEdgesOnDrag: true,
          selectConnectedEdges: true,
          navigationButtons: false,
        },
      };

      networkRef.current = new Network(container, { nodes, edges }, options);
      networkRef.current.once("stabilizationIterationsDone", () => {
        networkRef.current?.fit({
          animation: { duration: 320, easingFunction: "easeInOutQuad" },
        });
      });
      // 평소엔 관계명 숨기고 hover/선택 시에만 표시
      attachEdgeLabelReveal(networkRef.current, edges, edgeLabelById);
      // 그래프 노드와 요약 문장의 동일 명사를 양방향으로 강조한다.
      networkRef.current.on("hoverNode", (params) => setHoveredNodeId(String(params.node)));
      networkRef.current.on("blurNode", () => setHoveredNodeId(null));
      networkRef.current.on("selectNode", (params) => {
        const id = params.nodes && params.nodes[0];
        if (id != null) setSelectedNodeId(String(id));
      });
      networkRef.current.on("deselectNode", () => setSelectedNodeId(null));
      // 노드 클릭 → 근거 탭 안의 상세 데이터를 미리 불러온다.
      networkRef.current.on("click", (params) => {
        const id = params.nodes && params.nodes[0];
        if (id != null && fetchDetailRef.current) fetchDetailRef.current(String(id));
      });
    }
    build();
    return () => { cancelled = true; };
  }, [graphData]);

  // 클릭한 노드의 전체 이웃을 조회해 상세 대시보드에 채운다.
  const fetchNodeDetail = useCallback(async (nodeId) => {
    if (!nodeId) return;
    setDetailNode(nodeId);
    setDetailMode("loading");
    setDetailData(null);
    try {
      const res = await fetch(`${API_BASE_URL}/node?name=${encodeURIComponent(nodeId)}`);
      const data = await res.json();
      setDetailData(data);
      const hasContent =
        (data.groups && data.groups.length > 0) ||
        (data.graph && data.graph.nodes && data.graph.nodes.length > 0);
      setDetailMode(hasContent ? "content" : "none");
    } catch (e) {
      setDetailMode("error");
      console.error(e);
    }
  }, []);
  useEffect(() => { fetchDetailRef.current = fetchNodeDetail; }, [fetchNodeDetail]);

  // 근거 탭에서 노드 상세를 펼쳤을 때 미니 서브그래프를 그린다.
  useEffect(() => {
    let cancelled = false;
    async function build() {
      const container = detailGraphRef.current;
      if (!container) return;
      const g = detailData && detailData.graph;
      if (!g || !g.nodes || g.nodes.length === 0) { detailNetworkRef.current = null; return; }
      const vis = await import("vis-network/standalone");
      if (cancelled) return;
      const { Network, DataSet } = vis;
      const nodeInfoById = buildGraphNodeInfo(g.nodes, g.links);
      const nodes = new DataSet(g.nodes.map((n) => {
        const info = nodeInfoById.get(String(n.id)) || {};
        return {
          id: n.id,
          label: n.name,
          group: n.label,
          color: nodeColorFor(n.label),
          size: graphNodeSize(n, info, true),
          mass: graphNodeMass(n, info),
          title: `${n.label}: ${n.name}`,
        };
      }));
      const edgeLabelById = new Map();
      const edges = new DataSet(g.links.map((l, i) => {
        const id = `e${i}`;
        edgeLabelById.set(id, koRel(l.type));
        return {
          id,
          from: l.source,
          to: l.target,
          label: "",
          arrows: { to: { enabled: true, scaleFactor: 0.72 } },
          color: graphEdgeColor(l, nodeInfoById),
          width: graphEdgeWidth(l),
          length: graphEdgeLength(l, nodeInfoById, true),
        };
      }));
      const options = {
        nodes: {
          shape: "dot",
          size: 13,
          borderWidth: 1.5,
          font: {
            size: 11,
            face: "Pretendard",
            color: theme === "light" ? "#303846" : "#e7e9ee",
            strokeWidth: 3,
            strokeColor: theme === "light" ? "rgba(250, 250, 248, 0.9)" : "rgba(9, 10, 13, 0.86)",
          },
        },
        edges: {
          font: {
            size: 10,
            align: "middle",
            color: theme === "light" ? "#5f6673" : "#c9cdd6",
            strokeWidth: 4,
            strokeColor: theme === "light" ? "rgba(250, 250, 248, 0.88)" : "rgba(9, 10, 13, 0.82)",
            background: theme === "light" ? "rgba(255, 255, 255, 0.86)" : "rgba(17, 19, 24, 0.86)",
          },
          width: 1,
          selectionWidth: 1.35,
          smooth: { enabled: true, type: "continuous", roundness: 0.22 },
          shadow: false,
        },
        groups: GRAPH_NODE_GROUPS,
        physics: {
          enabled: true,
          stabilization: { enabled: true, iterations: 220, fit: true },
          barnesHut: { gravitationalConstant: -8500, centralGravity: 0.16, springLength: 112, springConstant: 0.035, damping: 0.42, avoidOverlap: 1 },
        },
        interaction: { hover: true, tooltipDelay: 120, hideEdgesOnDrag: true, selectConnectedEdges: true },
      };
      detailNetworkRef.current = new Network(container, { nodes, edges }, options);
      attachEdgeLabelReveal(detailNetworkRef.current, edges, edgeLabelById);
      detailNetworkRef.current.on("click", (params) => {
        const id = params.nodes && params.nodes[0];
        if (id != null && fetchDetailRef.current) fetchDetailRef.current(String(id));
      });
    }
    build();
    return () => { cancelled = true; };
  }, [detailData, activeTab, evidenceOpen.node]);

  const closeDetail = () => {
    setDetailNode(null);
    setDetailData(null);
    setDetailMode("empty");
    setSelectedNodeId(null);
    setHoveredNodeId(null);
    if (networkRef.current) networkRef.current.unselectAll();
    detailNetworkRef.current = null;
  };

  const activeNodeId = hoveredNodeId || selectedNodeId;
  const activeNodeName = useMemo(() => (
    (graphData?.nodes || []).find((node) => String(node.id) === String(activeNodeId))?.name || ""
  ), [activeNodeId, graphData]);
  const graphMentionModel = useMemo(() => {
    const byText = new Map();
    for (const node of graphData?.nodes || []) {
      const id = String(node.id);
      const variants = [...new Set([node.name, node.raw_name, node.id]
        .map((value) => String(value || "").trim())
        .filter((value) => value.length >= 2))];
      for (const variant of variants) {
        const key = variant.toLocaleLowerCase("ko-KR");
        if (!byText.has(key)) byText.set(key, { id, name: node.name || variant });
      }
    }
    const variants = [...byText.keys()].sort((a, b) => b.length - a.length);
    return {
      byText,
      source: variants.length ? `(${variants.map(escapeRegExp).join("|")})` : "",
    };
  }, [graphData]);

  const selectGraphNode = useCallback((nodeId) => {
    const id = String(nodeId);
    setSelectedNodeId(id);
    setHoveredNodeId(null);
    if (networkRef.current) {
      networkRef.current.selectNodes([id]);
      networkRef.current.focus(id, {
        scale: 1.12,
        animation: { duration: 420, easingFunction: "easeInOutQuad" },
      });
    }
    if (fetchDetailRef.current) fetchDetailRef.current(id);
  }, []);

  const renderGraphLinkedText = useCallback((text) => {
    const value = String(text || "");
    if (!graphMentionModel.source) return value;

    const pattern = new RegExp(graphMentionModel.source, "giu");
    const output = [];
    let cursor = 0;
    let index = 0;
    for (const match of value.matchAll(pattern)) {
      const start = match.index ?? 0;
      if (start > cursor) output.push(value.slice(cursor, start));
      const shown = match[0];
      const node = graphMentionModel.byText.get(shown.toLocaleLowerCase("ko-KR"));
      if (!node) {
        output.push(shown);
      } else {
        output.push(
          <button
            type="button"
            className={`node-mention${activeNodeId === node.id ? " active" : ""}`}
            key={`${node.id}-${start}-${index}`}
            aria-pressed={activeNodeId === node.id}
            title={`${node.name} 노드와 연결`}
            onMouseEnter={() => setHoveredNodeId(node.id)}
            onMouseLeave={() => setHoveredNodeId(null)}
            onFocus={() => setHoveredNodeId(node.id)}
            onBlur={() => setHoveredNodeId(null)}
            onClick={() => selectGraphNode(node.id)}
          >
            {shown}
          </button>
        );
      }
      cursor = start + shown.length;
      index += 1;
    }
    if (cursor < value.length) output.push(value.slice(cursor));
    return output;
  }, [activeNodeId, graphMentionModel, selectGraphNode]);

  const submitQuestion = useCallback(async (raw) => {
    const q = (raw || "").trim();
    if (!q) return;

    setActive(true);
    setHistory((h) => [q, ...h]);
    // 질문을 채팅 로그에 즉시 추가(답변은 대기 상태) + 요약 탭으로 전환
    setChat((c) => [...c, { q, a: null }]);
    setActiveTab("summary");

    setAnswerMode("loading");
    setDetailedAnswerText("");
    setChunks(null);
    setArticleEvidence(null);
    setRdbMode("loading");
    setRdb(null);
    setRdbScope(null);
    setRdbEntityIds({ players: [], teams: [] });
    setRdbTruncated(false);
    setRdbTotalFacts(0);
    setRdbFactsList([]);
    setGraphData(null);
    setSelectedNodeId(null);
    setHoveredNodeId(null);
    setDetailNode(null);
    setDetailData(null);
    setDetailMode("empty");
    setEvidenceOpen({ node: false, article: false, kg: false, rdb: false });
    setFollowUpSuggestions([]);
    setQueryStatus("분석 중...");
    setApiStatus({ text: "API 요청 중", state: "busy" });
    setAnalysisState("분석중");

    try {
      const res = await fetch(`${API_BASE_URL}/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `API ${res.status}`);

      const graphFacts = Array.isArray(data.graph_facts) ? data.graph_facts : [];
      const ch = graphFacts.length
        ? graphFacts.map(formatGraphFact)
        : (data.related_chunks || []);
      const evidence = data.article_evidence || [];
      const facts = data.rdb_facts || [];

      const finalAnswer = stripMarkdown(data.summary_answer || data.answer)
        || "답변을 생성하지 못했습니다.";
      const detailedAnswer = stripMarkdown(data.detailed_answer || data.answer)
        || finalAnswer;
      setAnswerText(finalAnswer);
      setDetailedAnswerText(detailedAnswer);
      setAnswerMode("text");
      // 채팅 로그의 마지막(방금 질문한) 메시지에 답변 채우기
      setChat((c) => {
        if (!c.length) return c;
        const next = [...c];
        next[next.length - 1] = { ...next[next.length - 1], a: finalAnswer };
        return next;
      });
      setChunks(ch);
      setArticleEvidence(evidence);
      setRdbFactsList(facts);
      setGraphData(data.graph || null);
      setCoreTarget(pickCoreTarget(data, q));
      setFollowUpSuggestions(buildFollowUpSuggestions(data));

      const rdbAnswerText = (data.rdb_answer || "").trim();
      setRdb(data);
      setRdbScope(data.rdb_scope || null);
      setRdbEntityIds(data.rdb_entity_ids || { players: [], teams: [] });
      setRdbTruncated(Boolean(data.rdb_truncated));
      setRdbTotalFacts(Number(data.rdb_total_facts || facts.length));
      setRdbMode(!rdbAnswerText && facts.length === 0 ? "none" : "content");
      const resolvedPeriod = data.rdb_scope?.label;
      setPeriod(resolvedPeriod && resolvedPeriod !== "전체 기간" ? resolvedPeriod : "전체 기간");

      setAnalysisState("완료");
      setQueryStatus(
        `질문 분석 완료${resolvedPeriod && resolvedPeriod !== "전체 기간" ? ` · ${resolvedPeriod}` : ""}: ${q}`
      );
      setApiStatus({ text: "API 연결 완료", state: "on" });
    } catch (e) {
      const errText = "오류가 발생했습니다. FastAPI 서버가 실행 중인지 확인해주세요.";
      setAnswerText(errText);
      setDetailedAnswerText("");
      setAnswerMode("error");
      setChat((c) => {
        if (!c.length) return c;
        const next = [...c];
        next[next.length - 1] = { ...next[next.length - 1], a: errText };
        return next;
      });
      setChunks([]);
      setArticleEvidence([]);
      setRdbMode("error");
      setQueryStatus("오류 발생");
      setApiStatus({ text: "API 오류", state: "err" });
      setAnalysisState("오류");
      console.error(e);
    }
  }, []);

  const askHero = () => {
    if (!question.trim()) { alert("질문을 입력해주세요."); return; }
    submitQuestion(question);
  };

  const askFromPopup = () => {
    if (!followUp.trim()) { alert("질문을 입력해주세요."); return; }
    const q = followUp;
    setFollowUp("");
    submitQuestion(q);
  };

  const fitGraph = () => { if (networkRef.current) networkRef.current.fit({ animation: { duration: 500 } }); };
  const [highlightedCite, setHighlightedCite] = useState(null);
  const closeTimerRef = useRef(null);
  const flashTimerRef = useRef(null);

  // 팝업 닫힘을 살짝 지연시켜, 숫자 → 팝업으로 마우스를 옮기는 사이에 안 사라지게 한다.
  const cancelCitationClose = useCallback(() => {
    if (closeTimerRef.current) {
      clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
  }, []);
  const scheduleCitationClose = useCallback(() => {
    cancelCitationClose();
    closeTimerRef.current = setTimeout(() => setCitationPopup(null), 220);
  }, [cancelCitationClose]);

  const showCitationPopup = useCallback((event, ref) => {
    cancelCitationClose();
    const rect = event.currentTarget.getBoundingClientRect();
    const width = 340;
    const height = 260;
    const left = Math.min(window.innerWidth - width - 16, Math.max(16, rect.left));
    const below = rect.bottom + 10;
    const above = rect.top - height - 10;
    const top = below + height < window.innerHeight ? below : Math.max(16, above);

    setCitationPopup({
      left,
      top,
      number: ref.number,
      source: ref.sourceLabel,
      text: ref.tip || "",
      type: ref.type,
    });
  }, [cancelCitationClose]);

  // 주석 번호 클릭 → 근거 탭으로 전환 후 해당 번호 카드로 스크롤 + 잠깐 하이라이트
  const goToEvidence = useCallback((number) => {
    setCitationPopup(null);
    setActiveTab("evidence");
    setHighlightedCite(number);
    const articleCount = articleEvidence?.length || 0;
    const kgCount = chunks?.length || 0;
    const section = number <= articleCount
      ? "article"
      : number <= articleCount + kgCount ? "kg" : "rdb";
    setEvidenceOpen((current) => ({ ...current, [section]: true }));
    // 탭과 접힌 섹션이 렌더된 다음 스크롤한다.
    setTimeout(() => {
      const el = document.getElementById(`evidence-${number}`);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 140);
    if (flashTimerRef.current) clearTimeout(flashTimerRef.current);
    flashTimerRef.current = setTimeout(() => setHighlightedCite(null), 1800);
  }, [articleEvidence, chunks]);

  const evidenceItems = useMemo(() => {
    const items = [
      ...(articleEvidence || []).map((e) => ({
        type: "article",
        sourceLabel: "기사",
        title: e.title || "기사 원문",
        tip: `${e.title || "기사 원문"}\n\n${e.excerpt || ""}`,
        searchText: `${e.title || ""} ${e.excerpt || ""} ${e.matched_chunk || ""}`,
      })),
      ...(chunks || []).map((chunk) => ({
        type: "kg",
        sourceLabel: "KG 관계",
        title: "지식그래프 관계",
        tip: chunk,
        searchText: chunk,
      })),
      ...rdbFactsList.map((fact) => ({
        type: "rdb",
        sourceLabel: "RDB",
        title: "정형 데이터 근거",
        tip: fact,
        searchText: fact,
      })),
    ];

    return items.map((item, i) => ({
      ...item,
      number: i + 1,
      tokenSet: new Set(tokenizeEvidenceText(item.searchText)),
    }));
  }, [articleEvidence, chunks, rdbFactsList]);

  const citedDetailedAnswer = useMemo(() => {
    if (evidenceItems.length === 0) return renderGraphLinkedText(detailedAnswerText);

    return splitAnswerSentences(detailedAnswerText).map((part, index) => {
      if (!part.trim()) return part;
      // [기사 기반] 같은 섹션 헤더는 앞에 여백을 주고, 주석은 붙이지 않는다.
      const isSection = /^\[[^\]]+\]$/.test(part.trim());
      const cls = "answer-sentence" + (isSection ? " answer-section" : "");
      const refs = isSection ? [] : pickSentenceEvidence(part, evidenceItems);
      if (refs.length === 0) {
        return (
          <span className={cls} key={`${index}-${part.slice(0, 12)}`}>
            {renderGraphLinkedText(part)}
          </span>
        );
      }

      const endingMatch = part.match(/([.!?。！？]+)$/);
      const ending = endingMatch?.[1] || "";
      const sentenceBody = ending ? part.slice(0, -ending.length) : part;

      return (
        <span className={cls} key={`${index}-${part.slice(0, 12)}`}>
          {renderGraphLinkedText(sentenceBody)}
          <span className="sentence-ending">
            {ending}
            <span className="citations">{refs.map((ref) => (
              <span
                key={ref.number}
                className={`cite ${ref.type}`}
                style={{ cursor: "pointer" }}
                data-source={ref.sourceLabel}
                data-tip={(ref.tip || "").slice(0, 520)}
                title="클릭하면 근거로 이동"
                onMouseEnter={(event) => showCitationPopup(event, ref)}
                onMouseLeave={scheduleCitationClose}
                onFocus={(event) => showCitationPopup(event, ref)}
                onBlur={scheduleCitationClose}
                onClick={() => goToEvidence(ref.number)}
                onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); goToEvidence(ref.number); } }}
                tabIndex={0}
              >({ref.number})</span>
            ))}</span>
          </span>
        </span>
      );
    });
  }, [detailedAnswerText, evidenceItems, renderGraphLinkedText, showCitationPopup]);

  // 상단 통계 chip 값
  const eventCount = graphData?.links?.length || 0;
  const periodLabel = period === "전체 기간" ? "2025" : period;

  // RDB 표시값
  const rdbAnswer = stripMarkdown((rdb?.rdb_answer || "").trim());
  const rdbFacts = rdb?.rdb_facts || [];
  const entities = rdb?.rdb_entities || { players: [], teams: [] };
  const ents = [...(entities.players || []), ...(entities.teams || [])];
  const hasRdbEvidence = rdbMode === "content" && (rdbAnswer || rdbFacts.length > 0 || ents.length > 0);

  return (
    <>
      {citationPopup && (
        <div
          className={`evidence-popover ${citationPopup.type}`}
          style={{ left: citationPopup.left, top: citationPopup.top, maxHeight: 300, display: "flex", flexDirection: "column" }}
          onMouseEnter={cancelCitationClose}
          onMouseLeave={scheduleCitationClose}
        >
          <div
            className="evidence-popover-head"
            style={{ cursor: "pointer" }}
            title="클릭하면 근거로 이동"
            onClick={() => goToEvidence(citationPopup.number)}
          >
            <span>({citationPopup.number})</span>
            <strong>{citationPopup.source}</strong>
          </div>
          <div className="evidence-popover-body" style={{ overflowY: "auto", flex: 1 }}>{citationPopup.text}</div>
        </div>
      )}
      <header>
        <a className="brand" href="/" aria-label="LCK Knowledge Graph Dashboard 메인 화면으로 이동">
          <Image
            className="brand-image"
            src="/lck-knowledge-graph-dashboard.png"
            alt="LCK Knowledge Graph Dashboard"
            width={2756}
            height={196}
            priority
          />
        </a>
        <div className="header-right">
          <div className={"status-pill" + (apiStatus.state ? " " + apiStatus.state : "")}>
            <span className="live"></span>
            <span>{apiStatus.text}</span>
          </div>
        </div>
      </header>

      <main className={"app" + (active ? " active" : "")}>
        {/* 가운데 그래프 패널 */}
        <section className="panel graph-panel">
          <div className="main-title">
            <h2>지식그래프 시각화</h2>
            <span className="status">{queryStatus}</span>
          </div>

          <div className="graph-controls">
            <div className="stat-chips">
              {/*지식그래프 시각화 부분*/}
              <span className="stat-chip"><Icon src="/icons/navigation/target.png" size={20} />핵심 분석 대상 <b>{coreTarget}</b></span>
              <span className="stat-chip"><Icon src="/icons/navigation/chain.png" size={20} />관련 경기·이벤트 수 <b>{eventCount}</b></span>
              <span className="stat-chip"><Icon src="/icons/navigation/calendar.png" size={20} />데이터 분석 기간 <b>{periodLabel}</b></span>
              <span className="stat-chip"><Icon src="/icons/navigation/article.png" size={20} />참조 원본 기사 수 <b>{articleEvidence ? articleEvidence.length : 0}</b></span>
            </div>
          </div>

          <div className="graph-wrap">
                      {/*
            <div className="graph-objective-chip">
              <span className="target-icon"></span>
              <span>Objective: <b>{coreTarget}</b></span>
            </div>
            */}
            <div className="graph-toolbar">
              <button className="tip" data-tip="화면에 맞추기" onClick={fitGraph}>⤢</button>
            </div>
            <div className="graph-box" ref={graphRef}></div>
            {graphData && (!graphData.nodes || graphData.nodes.length === 0) && (
              <div style={{ position: "absolute", inset: 0, display: "grid", placeItems: "center", color: "#9ca3af" }}>
                그래프 데이터가 없습니다.
              </div>
            )}
            <div className={`legend graph-legend ${legendCollapsed ? 'collapsed' : ''}`}>
              <button className="legend-collapse-btn" onClick={() => setLegendCollapsed(!legendCollapsed)} title={legendCollapsed ? "펼치기" : "접기"}>
                {legendCollapsed ? '>' : '<'}
              </button>
              {!legendCollapsed && (
                <>
                  <span><span className="dot" style={{ background: GRAPH_NODE_THEMES.Team.base }}></span>Team</span>
                  <span><span className="dot" style={{ background: GRAPH_NODE_THEMES.Player.base }}></span>Player</span>
                  <span><span className="dot" style={{ background: GRAPH_NODE_THEMES.Champion.base }}></span>Champion</span>
                  <span><span className="dot" style={{ background: GRAPH_NODE_THEMES.Tournament.base }}></span>Tournament</span>
                  <span><span className="dot" style={{ background: GRAPH_NODE_THEMES.League.base }}></span>League</span>
                </>
              )}
            </div>
          </div>
        </section>

        {/* 챗봇 패널 */}
        <section className="panel chat-panel">
          {/* 초기 화면(①) 입력 영역 */}
          <div className="hero-only">
            <div className="hero-title">
              <h2>무엇이 궁금하신가요?</h2>
              <p>2025 LCK에 대해 궁금한 점이 있다면 질문해보세요.</p>
            </div>

            <textarea
              id="question"
              placeholder="예: 젠지 우승 전략을 알려줘"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
            />
            <button className="ask-btn" onClick={askHero}><Icon src="/icons/talking/search.png" size={20} isFollowTheme={false} />질문하기</button>

            <div className="suggestions">
                          {/* 첫 페이지의 "추천 질문" */}
              <h2 className="section-title mt">추천 질문</h2>
              {SUGGESTIONS.map((s) => (
                <button key={s} onClick={() => setQuestion(s)}>{s}</button>
              ))}
            </div>
          </div>

          {/* 결과 영역(②) — 요약 / 근거 탭 전환 */}
          <div className="results">
            {/* 상단 탭 */}
            <div className="chat-tabs">
              <button
                className={"chat-tab-btn" + (activeTab === "summary" ? " active" : "")}
                onClick={() => setActiveTab("summary")}
              >요약</button>
              <button
                className={"chat-tab-btn" + (activeTab === "evidence" ? " active" : "")}
                onClick={() => setActiveTab("evidence")}
              >근거{evidenceItems.length ? ` (${evidenceItems.length})` : ""}</button>
            </div>

            {activeTab === "summary" ? (
              <>
                <div className={`graph-text-link-status${activeNodeName ? " active" : ""}`}>
                  <span className="graph-text-link-dot" aria-hidden="true"></span>
                  {activeNodeName
                    ? <><strong>{activeNodeName}</strong> 노드와 답변 문장을 연결해 표시 중입니다.</>
                    : "그래프 노드나 답변 속 명사를 선택하면 서로 연결해 강조합니다."}
                </div>
                {/* 채팅 로그 (누적) */}
                <div className="chat-log">
                  {chat.map((m, i) => {
                    const isLast = i === chat.length - 1;
                    return (
                      <div className="chat-msg" key={i}>
                        <div className="chat-q">{m.q}</div>
                        <div className={"chat-a answer-box" + (m.a === null ? " loading" : "")}>
                          {m.a === null ? (
                            <><span className="spinner"></span>AI가 답변을 생성 중입니다...</>
                          ) : isLast && answerMode !== "error" ? (
                            renderGraphLinkedText(answerText)
                          ) : (
                            m.a
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>

                {/* 입력 영역 */}
                <div className="results-bottom">
                  <div className="followup-suggestions">
                    <button
                      className="suggestions-toggle-btn"
                      onClick={() => setSuggestionsCollapsed(!suggestionsCollapsed)}
                      title={suggestionsCollapsed ? "추천 질문 펼치기" : "추천 질문 접기"}
                    >
                      {suggestionsCollapsed ? '▼' : '▲'} 추천 질문
                    </button>
                    {!suggestionsCollapsed && (
                      <div className="suggestions-list">
                        {(followUpSuggestions.length > 0 ? followUpSuggestions.slice(0, 2) : SUGGESTIONS.slice(0, 2)).map((s) => (
                          <button key={s} className="followup-chip" title={s} onClick={() => submitQuestion(s)}>{s}</button>
                        ))}
                      </div>
                    )}
                  </div>
                  <textarea
                    id="popupQuestion"
                    placeholder="이어서 질문하기... (Enter 질문, Shift+Enter 줄바꿈)"
                    value={followUp}
                    onChange={(e) => setFollowUp(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); askFromPopup(); } }}
                  />
                                  <button className="ask-btn" onClick={askFromPopup}><Icon src="/icons/talking/search.png" size={20} isFollowTheme={false} />질문하기</button>
                </div>
              </>
            ) : (
              /* 근거 탭 — 최신 답변의 근거 */
              <div className="evidence-pane">
                <div className="evidence-intro">
                  <strong>답변 근거</strong>
                  <span>상세 해설을 읽고 원본 근거는 필요한 항목만 펼쳐볼 수 있습니다.</span>
                </div>

                <section className="evidence-explanation">
                  <div className="evidence-explanation-head">
                    <span>상세 해설</span>
                    {rdbScope?.label && rdbScope.label !== "전체 기간" && (
                      <span className="scope-badge">{rdbScope.label}</span>
                    )}
                  </div>
                  {detailedAnswerText ? (
                    <div className="evidence-explanation-body">
                      {citedDetailedAnswer}
                    </div>
                  ) : (
                    <div className="evidence-empty">질문하면 필터링된 근거를 바탕으로 상세 해설이 표시됩니다.</div>
                  )}
                </section>

                <EvidenceSection
                  title={detailNode ? `선택 노드 상세 · ${detailData?.name || detailNode}` : "선택 노드 상세 보기"}
                  count={detailData?.groups?.length || 0}
                  tone="node"
                  open={evidenceOpen.node}
                  onToggle={() => setEvidenceOpen((value) => ({ ...value, node: !value.node }))}
                >
                  {!detailNode && (
                    <div className="evidence-empty">왼쪽 그래프에서 노드를 선택하면 연결 관계가 여기에 표시됩니다.</div>
                  )}
                  {detailMode === "loading" && (
                    <div className="detail-box loading"><span className="spinner"></span>상세 정보를 불러오는 중...</div>
                  )}
                  {detailMode === "error" && (
                    <div className="detail-box empty">상세 정보를 불러오지 못했습니다. FastAPI 서버가 실행 중인지 확인해주세요.</div>
                  )}
                  {detailMode === "none" && (
                    <div className="detail-box empty">이 노드에 연결된 상세 정보가 없습니다.</div>
                  )}
                  {detailMode === "content" && (
                    <>
                      <div className="detail-head evidence-detail-head">
                        <div>
                          <span className="detail-kicker">노드 상세 보기</span>
                          <h2>
                            {detailData?.name || detailNode}
                            {detailData?.label && <span className="detail-label-tag">{detailData.label}</span>}
                          </h2>
                        </div>
                        <button className="detail-close" onClick={closeDetail}>선택 해제</button>
                      </div>
                      <div className="detail-summary">
                        {(detailData?.groups || []).slice(0, 8).map((grp) => (
                          <span className="detail-summary-chip tip" key={grp.relation} data-tip={grp.relation_ko}>
                            <span className="detail-summary-emoji">{relEmoji(grp.relation_ko)}</span>
                            {grp.relation_ko}
                            <b>{grp.count}</b>
                          </span>
                        ))}
                      </div>
                      <div className="detail-grid">
                        <div className="detail-graph-col">
                          <h3 className="detail-sub">연결 그래프</h3>
                          <div className="detail-graph-box" ref={detailGraphRef}></div>
                          <p className="detail-hint">그래프 속 대상을 클릭하면 그 대상을 중심으로 다시 불러옵니다.</p>
                        </div>
                        <div className="detail-cards-col">
                          {(detailData?.groups || []).map((grp) => (
                            <div className="detail-group" key={grp.relation}>
                              <div className="detail-group-head">
                                <span className="detail-group-title">{grp.relation_ko}</span>
                                <span className="detail-group-count">{grp.count}</span>
                              </div>
                              <div className="detail-chips">
                                {grp.items.map((it) => (
                                  <button
                                    className="detail-chip"
                                    key={it.id || it.name}
                                    title={`${it.label} · ${it.name} (클릭해 이동)`}
                                    onClick={() => fetchNodeDetail(it.id || it.name)}
                                  >
                                    {it.name}
                                  </button>
                                ))}
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    </>
                  )}
                </EvidenceSection>

                <EvidenceSection
                  title="기사 원문 근거"
                  count={articleEvidence?.length || 0}
                  tone="article"
                  open={evidenceOpen.article}
                  onToggle={() => setEvidenceOpen((value) => ({ ...value, article: !value.article }))}
                >
                  {articleEvidence === null ? (
                    <div className="evidence-empty">질문하면 최신 답변의 기사 근거가 표시됩니다.</div>
                  ) : articleEvidence.length === 0 ? (
                    <div className="evidence-empty">관련 기사 원문 근거를 찾지 못했습니다.</div>
                  ) : (
                    articleEvidence.map((item, i) => (
                      <div
                        className={`chunk-card article-card${highlightedCite === i + 1 ? " cite-highlight" : ""}`}
                        id={`evidence-${i + 1}`}
                        key={`${item.article_id || "article"}-${item.chunk_id || i}`}
                      >
                        <h4>({i + 1}) {renderGraphLinkedText(item.title)}</h4>
                        <blockquote>{renderGraphLinkedText(item.excerpt)}</blockquote>
                        <div className="article-meta">
                          {[item.provider, item.published_at].filter(Boolean).join(" · ")}
                        </div>
                      </div>
                    ))
                  )}
                </EvidenceSection>

                <EvidenceSection
                  title="KG 관계 근거"
                  count={chunks?.length || 0}
                  tone="kg"
                  open={evidenceOpen.kg}
                  onToggle={() => setEvidenceOpen((value) => ({ ...value, kg: !value.kg }))}
                >
                  {chunks === null ? (
                    <div className="evidence-empty">질문하면 화면 그래프와 동일한 관계가 표시됩니다.</div>
                  ) : chunks.length === 0 ? (
                    <div className="evidence-empty">질문과 직접 연결된 KG 관계를 찾지 못했습니다.</div>
                  ) : (
                    chunks.map((chunk, i) => {
                      const num = (articleEvidence?.length || 0) + i + 1;
                      return (
                        <div
                          className={`chunk-card kg-card${highlightedCite === num ? " cite-highlight" : ""}`}
                          id={`evidence-${num}`}
                          key={`${num}-${chunk}`}
                        >
                          <h4>({num}) 그래프 관계</h4>
                          <p>{renderGraphLinkedText(chunk)}</p>
                        </div>
                      );
                    })
                  )}
                </EvidenceSection>

                <EvidenceSection
                  title="RDB 통계 근거"
                  count={rdbTotalFacts || rdbFacts.length}
                  tone="rdb"
                  open={evidenceOpen.rdb}
                  onToggle={() => setEvidenceOpen((value) => ({ ...value, rdb: !value.rdb }))}
                >
                  {rdbScope && (
                    <div className="rdb-filter-card">
                      <strong>적용된 RDB 필터</strong>
                      <div className="rdb-filter-chips">
                        {(rdbScope.filters_applied || [])
                          .filter((filter) => !/^(팀|선수) ID:/.test(filter))
                          .map((filter) => (
                          <span key={filter}>{filter}</span>
                        ))}
                        {(rdbEntityIds.teams || []).map((team) => (
                          <span key={`team-${team.team_id}`}>team_id {team.team_id} · {team.team_name}</span>
                        ))}
                        {(rdbEntityIds.players || []).map((player) => (
                          <span key={`player-${player.player_id}`}>player_id {player.player_id} · {player.player_name}</span>
                        ))}
                      </div>
                    </div>
                  )}
                  {rdbMode === "loading" ? (
                    <div className="rdb-box loading"><span className="spinner"></span>RDB 통계를 조회 중입니다...</div>
                  ) : hasRdbEvidence ? (
                    <>
                      <div className="rdb-box">{renderGraphLinkedText(rdbAnswer || "RDB 통계 답변이 없습니다.")}</div>
                      {ents.length > 0 && (
                        <div className="rdb-card"><strong>확인된 엔티티</strong><br />{renderGraphLinkedText(ents.join(", "))}</div>
                      )}
                      {rdbFacts.map((fact, i) => {
                        const num = (articleEvidence?.length || 0) + (chunks?.length || 0) + i + 1;
                        return (
                          <div
                            className={`rdb-card${highlightedCite === num ? " cite-highlight" : ""}`}
                            id={`evidence-${num}`}
                            key={i}
                          >
                            <strong>({num}) RDB</strong><br />
                            {renderGraphLinkedText(fact)}
                          </div>
                        );
                      })}
                      {rdbTruncated && (
                        <div className="rdb-truncated">
                          관련 근거 {rdbTotalFacts}개 중 화면·요약 한도에 맞춘 일부만 표시했습니다.
                        </div>
                      )}
                    </>
                  ) : (
                    <div className="evidence-empty">질문과 직접 연결된 RDB 통계를 찾지 못했습니다.</div>
                  )}
                </EvidenceSection>
              </div>
            )}
          </div>
        </section>
      </main>
    </>
  );
}
