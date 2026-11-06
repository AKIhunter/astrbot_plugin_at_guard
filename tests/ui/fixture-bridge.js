(() => {
  "use strict";

  const query = new URLSearchParams(location.search);
  const calls = [];
  let modelCalls = 0;
  let outageState = query.get("outage") || "healthy";
  const clone = value => structuredClone(value);
  const ok = data => ({ status: "ok", data: clone(data) });
  const fail = message => Promise.reject(new Error(message));
  const requestId = "0123456789abcdef0123456789abcdef";
  const legacyRequestId = "fedcba9876543210fedcba9876543210";
  const middleInput = [
    "MIDDLE-ONLY",
    "{",
    '  "recent_messages_old_to_new": [{"sender_name": "甲", "text": "前文"}],',
    '  "current_message": {"sender_name": "乙", "text": "请保留  两个空格 <tag>"}',
    "}",
  ].join("\n");
  const legacyFull = "LEGACY-FULL-ONLY\n旧格式正文 {不完整\n仍需完整保留";

  function outage(state = outageState) {
    const active = !["disabled", "healthy"].includes(state);
    const recovering = state === "recovering";
    return {
      schema_version: 1,
      enabled: state !== "disabled",
      state: state === "error" || state === "loading" ? "healthy" : state,
      read_health: state === "error" ? "error" : "ok",
      cooldown_minutes: 10,
      cooldown_remaining_seconds: state === "cooling" ? 541 : null,
      provider_display: "合成文字判定 · fixture-text-small",
      admins: ["fixture-admin-a", "fixture-admin-b"],
      last_failure_at: active || recovering ? 1793948400 : null,
      last_error_category: active || recovering ? "timeout" : null,
      last_recovered_at: recovering ? 1793949000 : null,
      affected_session_count: active || recovering ? 7 : 0,
      backfill: recovering
        ? { completed: 2, no_reply: 1, processing: 1, pending: 1, retry_pending: 1, send_unknown: 1, unavailable: 0 }
        : { completed: 0, no_reply: 0, processing: 0, pending: active ? 7 : 0, retry_pending: 0, send_unknown: 0, unavailable: 0 },
    };
  }

  const config = {
    classifier_outage_recovery_enabled: outageState !== "disabled",
    participation_confidence_threshold: 50,
    classifier_provider_id: "fixture-provider",
    classifier_timeout_seconds: 3.5,
    gate_min_length: 2,
    vision_provider_id: "",
    gate_image_cache_ttl_seconds: 300,
    vision_timeout_seconds: 6,
    gate_main_fallback_enabled: false,
    gate_main_fallback_timeout_seconds: 6,
    gate_context_messages: 8,
    gate_context_max_age_seconds: 0,
    gate_memory_timeout_seconds: 1.5,
    participation_context_messages: 12,
    gate_hourly_budget: 120,
    gate_vision_hourly_budget: 20,
    gate_total_timeout_seconds: 18,
    fragment_wait_seconds: 3,
    fragment_wait_private: false,
    proactive_cooldown_seconds: 8,
    duplicate_window_seconds: 60,
    gate_reply_max_age_seconds: 30,
    gate_congling_prepare_compat: true,
    reply_typing_enabled: true,
    reply_typing_chars_per_second: 4,
    expression_takeover_enabled: true,
    reply_quote_enabled: true,
    reply_mention_enabled: false,
    expression_strip_emoji: true,
    understand_expand_forward: true,
    understand_forward_max_nodes: 100,
    understand_forward_max_depth: 4,
    understand_forward_summary_threshold: 1600,
    understand_quote_depth: 4,
    understand_card_parse: true,
    understand_image_enabled: false,
    understand_image_provider_id: "",
    understand_voice_enabled: false,
    understand_voice_provider_id: "",
    reply_recall_group: true,
    reply_recall_private: false,
    bot_names: ["小绫", "绫"],
    empty_mention_mode: "fixed",
    empty_mention_reply: "在呢，继续说。",
    empty_mention_persona_prompt: "",
    apply_to_private: false,
  };

  const structuredDecision = {
    ts: 1793949000,
    gid: "fixture:GroupMessage:10001",
    session_id: "fixture:GroupMessage:10001",
    group_id: "10001",
    request_id: requestId,
    message_id: "fixture-message-1",
    category: "public",
    mode: "enforce",
    action: "silent",
    src: "model",
    reason: "others_talking",
    score: 22,
    th: 50,
    ms: 128,
    mdl: "fixture-text-small",
    stage: "text",
    raw: '{"act":"silent","conf":22,"understood":true,"reason":"others_talking","ground":"none"}',
    ctx: "旧 ctx 不应成为新记录复制内容",
    understood: true,
    model_reason: "others_talking",
    attempts: [{
      stage: "text", model: "fixture-text-small", status: "ok", called: true,
      ms: 128, act: "silent", score: 22, reason: "others_talking", understood: true,
    }],
    timings: { version: 1, text: { ms: 128, calls: 1, status: "ok" } },
    chat_data: {
      recent_messages_old_to_new: [{ sender_name: "甲", text: "前文" }],
      reference_messages_old_to_new: [{ sender_name: "甲", text: "前文" }],
      reference_count: 1,
      current_message: {
        sender_name: "乙",
        sender_id: "20002",
        text: "请保留  两个空格 <tag>",
        session_id: "fixture:GroupMessage:10001",
        session_type: "group",
      },
      recent_bot_message: "机器人上一句（合成）",
      background_memory: ["本会话记忆（合成）"],
    },
    classifier_input: {
      schema_version: 1,
      source: "chat_data",
      segmented: true,
      notice: "中间输入来自本次保存的结构化 chat_data。",
      opening_rules: "OPENING-ONLY\n角色、安全边界与评分口径",
      middle_input: middleInput,
      ending_requirements: "ENDING-ONLY\n只输出一行 JSON",
    },
  };

  const legacyDecision = {
    ts: 1793948700,
    gid: "fixture:FriendMessage:30003",
    session_id: "fixture:FriendMessage:30003",
    private_qq: "30003",
    request_id: legacyRequestId,
    message_id: "fixture-message-legacy",
    category: "private",
    mode: "enforce",
    action: "reply",
    src: "model",
    reason: "useful",
    score: 78,
    th: 50,
    ms: 94,
    mdl: "fixture-text-small",
    stage: "text",
    raw: '{"act":"reply","conf":78,"understood":true,"reason":"useful","ground":"none"}',
    ctx: legacyFull,
    classifier_input: {
      schema_version: 1,
      source: "legacy_full",
      segmented: false,
      notice: "旧记录未能自动分段",
      opening_rules: "",
      middle_input: legacyFull,
      ending_requirements: "",
    },
  };

  const trace = {
    request_id: requestId,
    message_id: "fixture-message-1",
    state: "completed",
    truncated: false,
    coverage: {
      receive: { status: "registered" },
      decision: { status: "registered" },
      tool_start: { status: "unregistered" },
      native_tts: { status: "unsupported" },
      native_text_image: { status: "unverified" },
    },
    features: {
      receive: { status: "success", ms: 2, calls: 1 },
      input_parse: { status: "success", ms: 5, calls: 1 },
      text: { status: "success", ms: 128, calls: 1, reason: "others_talking" },
      main_model: { status: "skipped", calls: 0, reason: "model_silent" },
      tools: { status: "success", ms: 18, calls: 1 },
      image_api: {
        status: "failed", ms: 18, calls: 1,
        reason: "这是用于验证精简截断的一条很长合成原因，不包含真实请求或响应正文，也不会触发外部服务。",
      },
      typing: { status: "observed", ms: 12, calls: 1 },
      delivery: { status: "success", ms: 7, calls: 1 },
      lifecycle: { status: "completed", calls: 1 },
    },
    summary: { tool_calls: 1, image_api_calls: 1, sent_packets: 1 },
    events: [
      { at: 1793949000, feature: "image_api", status: "failed", reason: "fixture_failure", span_id: "fixture-span" },
      { at: 1793949001, feature: "delivery", status: "success" },
      { at: 1793949002, feature: "lifecycle", status: "completed" },
    ],
  };

  const legacyTrace = {
    request_id: legacyRequestId,
    message_id: "fixture-message-legacy",
    state: "completed",
    truncated: false,
    coverage: { receive: { status: "registered" }, decision: { status: "registered" } },
    features: {
      receive: { status: "success", calls: 1 },
      text: { status: "success", ms: 94, calls: 1 },
      main_model: { status: "success", ms: 40, calls: 1 },
      delivery: { status: "success", calls: 1 },
      lifecycle: { status: "completed", calls: 1 },
    },
    summary: { tool_calls: 0, image_api_calls: 0, sent_packets: 1 },
    events: [],
  };

  function stateData() {
    return {
      version: "fixture-task9",
      mode: "enforce",
      config,
      schema: {},
      fieldHelp: {},
      today: {
        day: "2026-11-06", average_score: 50, average_latency_ms: 111,
        judged: 2, rule_blocked: 0, model_calls: 0, timeouts: 0,
        parse_errors: 0, allowed: 1, silenced: 1, gen_skipped: 1,
      },
      lifetime: { total: 2, allowed: 1, blocked: 1, average: 50 },
      budget: {},
      image_cache: { hits: 0, entries: 0 },
      congling_compat: { detected: true, active: true, order: "after_guard", prepare_accounting: true },
      providers: [{
        id: "fixture-provider", display: "合成文字判定", model: "fixture-text-small",
        kind: "provider", role: "chat", supports_images: false, supports_audio: false,
      }],
      outage_recovery: outage(),
      distribution: {
        buckets: [0, 0, 1, 0, 0, 0, 0, 1, 0, 0], samples: 2,
        quantiles: { p50: 22, p75: 78, p90: 78 },
        current: { threshold: 50, rate: 50 }, model: "fixture-text-small",
        models: { "fixture-text-small": 2 }, excluded: 0, mixed: false, suggestions: [],
      },
      decisions: [structuredDecision, legacyDecision],
      listeners: trace.coverage,
    };
  }

  const nativeSegments = {
    config_id: "default",
    revision: "fixture-native-1",
    profiles: [{ id: "default", name: "默认配置（合成）" }],
    values: {
      only_llm_result: true,
      enable: true,
      second_period: false,
      split_words_enabled: false,
      split_words: [],
      max_segments: 4,
      split_mode: "regex",
      regex: "[。！？]",
      words_count_threshold: 400,
      content_cleanup_rule: "",
    },
  };
  const nativeSettings = {
    config_id: "default",
    revision: "fixture-settings-1",
    local_runner: false,
    fields: [],
    values: { tts_enabled: false },
    providers: [],
    personas: [],
    persona_fixed: false,
    fixed_persona_id: "",
    persona_error: false,
  };
  const flow = {
    note: "纯合成流程；模型调用 0，消息发送 0。",
    mode: {
      id: "enforce", title: "硬闸门", summary: "判断结论控制生成",
      decision_effect: "合成判断仅用于 UI 验证。", probe_note: "fixture 禁止真实模型调用。",
    },
    gate: {
      nodes: [
        { id: "local", title: "本地规则", status: "passed", explanation: "合成通过", evidence: {} },
        { id: "text", title: "文字判断", status: "passed", explanation: "合成通过", evidence: {} },
        { id: "finish", title: "技术结论", status: "blocked", explanation: "有效静默", evidence: {} },
      ],
      edges: [
        { source: "local", target: "text", label: "继续" },
        { source: "text", target: "finish", label: "静默" },
      ],
    },
    generate: {
      nodes: [
        { id: "main", title: "主模型", status: "skipped", explanation: "未调用", evidence: {} },
        { id: "format", title: "回复整理", status: "planned", explanation: "仅计划", evidence: {} },
        { id: "send", title: "最终发送", status: "planned", explanation: "不发送", evidence: {} },
      ],
      edges: [
        { source: "main", target: "format", label: "生成" },
        { source: "format", target: "send", label: "发送" },
      ],
    },
  };

  const bridge = {
    async apiGet(path, params = {}) {
      calls.push({ method: "GET", path, payload: clone(params) });
      if (path === "console/state") {
        if (outageState === "loading") {
          await new Promise(resolve => setTimeout(resolve, 450));
          outageState = "healthy";
        }
        return ok(stateData());
      }
      if (path === "console/decisions") {
        return ok({ decisions: [structuredDecision, legacyDecision], retained: 2, limit: 100 });
      }
      if (path === "console/request-trace") {
        if (params.request_id === requestId) return ok(trace);
        if (params.request_id === legacyRequestId) return ok(legacyTrace);
        return fail("未找到合成追踪");
      }
      if (path === "console/native-profile") return ok({ saved: true, config_id: "default" });
      if (path === "console/native-segments") return ok(nativeSegments);
      if (path === "console/native-settings") return ok(nativeSettings);
      if (path === "console/image-settings") {
        return ok({ available: false, reason: "合成环境不配置生图能力", revision: "fixture-image-1", fields: [], values: {} });
      }
      if (path === "console/flow") return ok(flow);
      return fail(`不存在的合成 GET 路由：${path}`);
    },
    async apiPost(path, payload = {}) {
      calls.push({ method: "POST", path, payload: clone(payload) });
      if (path === "console/native-profile") return ok({ saved: true, config_id: "default" });
      if (path === "console/config") {
        Object.assign(config, payload);
        if (Object.hasOwn(payload, "classifier_outage_recovery_enabled")) {
          outageState = payload.classifier_outage_recovery_enabled ? "healthy" : "disabled";
        }
        return ok(stateData());
      }
      if (path === "console/native-segments") {
        Object.assign(nativeSegments.values, payload.values || {});
        return ok(nativeSegments);
      }
      if (path === "console/native-settings") return ok(nativeSettings);
      if (path === "console/image-settings") {
        return ok({ available: false, reason: "合成环境不配置生图能力", revision: "fixture-image-1", fields: [], values: {} });
      }
      if (path === "console/reset") return ok(stateData());
      if (path === "console/probe") {
        modelCalls += 1;
        return fail("fixture 禁止调用文字试跑模型");
      }
      return fail(`不存在的合成 POST 路由：${path}`);
    },
  };

  Object.defineProperty(window, "__fixture", {
    value: {
      calls,
      middleInput,
      legacyFull,
      requestId,
      setOutageState(value) { outageState = value; },
      get modelCalls() { return modelCalls; },
      get state() { return clone(stateData()); },
    },
  });
  window.AstrBotPluginView = bridge;
  document.title = `[隔离 fixture] ${document.title}`;
})();
