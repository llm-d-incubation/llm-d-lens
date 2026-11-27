(function () {
  "use strict";

  const PALETTE = ["#4c9aff", "#2ec27e", "#f5a623", "#a78bfa", "#ef5350", "#38bdf8", "#f472b6"];
  const STATUS_COLORS = {
    healthy: "#2ec27e",
    queueing: "#f5a623",
    kv_pressure: "#fb923c",
    evicting: "#ef5350",
    error: "#dc2626",
    unknown: "#64748b",
  };
  const SVG_NS = "http://www.w3.org/2000/svg";

  let state = {
    data: null,
    page: "overview",
    windowMode: "profiling",
    latencyMetric: "all",
    latencyStats: ["p50", "mean", "p90"],
    coldLatencyMetric: "ttft",
    theme: "dark",
    errorFilter: "all",
    logSearch: "",
    logErrorsOnly: false,
    workloadIslHistMode: "count",
    workloadOslHistMode: "count",
    systemNode: null,
    systemNic: null,
    crosshairXRatio: null,
    crosshairSubscribers: [],
  };

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === undefined || v === null) continue;
        if (k === "className") node.className = v;
        else if (k === "textContent") node.textContent = String(v);
        else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2).toLowerCase(), v);
        else node.setAttribute(k, String(v));
      }
    }
    if (children !== undefined && children !== null) {
      const items = Array.isArray(children) ? children : [children];
      for (const c of items) {
        if (c === undefined || c === null) continue;
        node.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
      }
    }
    return node;
  }

  function makeSelect(attrs, options, currentVal) {
    const current = currentVal === undefined || currentVal === null ? "" : String(currentVal);
    const optNodes = (options || []).map((rawOpt) => {
      const opt = typeof rawOpt === "object" && rawOpt !== null ? rawOpt : { value: rawOpt, label: rawOpt };
      const val = opt.value === undefined || opt.value === null ? "" : String(opt.value);
      const label = opt.label === undefined || opt.label === null ? val : String(opt.label);
      const isSel = val === current;
      const optNode = el("option", {
        value: val,
        selected: isSel ? "selected" : null,
        textContent: label,
      });
      optNode.selected = isSel;
      return optNode;
    });
    const selectEl = el("select", attrs, optNodes);
    selectEl.value = current;
    return selectEl;
  }

  function svgEl(tag, attrs, children) {
    const node = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v !== undefined && v !== null) node.setAttribute(k, String(v));
      }
    }
    if (children) {
      for (const c of Array.isArray(children) ? children : [children]) {
        if (c) node.appendChild(c);
      }
    }
    return node;
  }

  function isSafeHttpUrl(raw) {
    if (typeof raw !== "string" || !raw.trim()) return false;
    try {
      const parsed = new URL(raw);
      return parsed.protocol === "http:" || parsed.protocol === "https:";
    } catch {
      return false;
    }
  }

  function fmtDec(v) {
    if (v === null || v === undefined || typeof v === "boolean" || (typeof v === "string" && v.trim() === "")) return "N/A";
    const n = Number(v);
    if (!Number.isFinite(n)) return "N/A";
    if (Number.isInteger(n)) {
      const cleanInt = n === 0 ? 0 : n;
      return cleanInt.toLocaleString(undefined, { maximumFractionDigits: 0 });
    }
    const rounded = Number(n.toFixed(2));
    const normalized = rounded === 0 ? 0 : rounded;
    return normalized.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function num(v) {
    return fmtDec(v);
  }

  function intFmt(v) {
    if (v === null || v === undefined || typeof v === "boolean" || (typeof v === "string" && v.trim() === "")) return "N/A";
    const n = Number(v);
    if (!Number.isFinite(n)) return "N/A";
    const r = Math.round(n);
    return (r === 0 ? 0 : r).toLocaleString();
  }

  function pct(v) {
    if (v === null || v === undefined || typeof v === "boolean" || (typeof v === "string" && v.trim() === "") || !Number.isFinite(Number(v))) return "N/A";
    return fmtDec(Number(v)) + "%";
  }

  function msFmt(v) {
    if (v === null || v === undefined || typeof v === "boolean" || (typeof v === "string" && v.trim() === "") || !Number.isFinite(Number(v))) return "N/A";
    const n = Number(v);
    if (Math.abs(n) >= 1000) return fmtDec(n / 1000) + " s";
    return fmtDec(n) + " ms";
  }

  function tpotFmt(v) {
    if (v === null || v === undefined || typeof v === "boolean" || (typeof v === "string" && v.trim() === "") || !Number.isFinite(Number(v))) return "N/A";
    return fmtDec(Number(v)) + " ms/tok";
  }

  function clampPct(v) {
    if (!isFiniteMetric(v)) return null;
    return Math.min(100, Math.max(0, Number(v)));
  }

  function axisNum(v) {
    if (v === null || v === undefined || typeof v === "boolean" || (typeof v === "string" && v.trim() === "") || !Number.isFinite(Number(v))) return "N/A";
    return fmtDec(Number(v));
  }

  function secFmt(v) {
    if (v === null || v === undefined || typeof v === "boolean" || (typeof v === "string" && v.trim() === "") || !Number.isFinite(Number(v))) return "N/A";
    return fmtDec(Number(v)) + " s";
  }

  function statVal(dist, key) {
    if (!dist || typeof dist !== "object") return null;
    const candidates = [key];
    if (key.endsWith("_ms")) candidates.push(key.slice(0, -3));
    else candidates.push(key + "_ms");
    if (key === "mean_ms" || key === "mean") candidates.push("avg", "mean", "mean_ms");
    for (const c of candidates) {
      if (dist[c] !== undefined && dist[c] !== null) return dist[c];
    }
    return null;
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#93a4bd";
  }

  const tooltip = document.getElementById("chart-tooltip");
  function placeTooltip(rawX, rawY) {
    const pad = 8;
    const tw = tooltip.offsetWidth || 320;
    const th = tooltip.offsetHeight || 80;
    let x = rawX + 12;
    let y = rawY + 12;
    if (x + tw > window.innerWidth - pad) {
      x = Math.max(pad, window.innerWidth - tw - pad);
    }
    if (y + th > window.innerHeight - pad) {
      y = Math.max(pad, rawY - th - 10);
    }
    tooltip.style.left = Math.max(pad, x) + "px";
    tooltip.style.top = Math.max(pad, y) + "px";
  }
  function showTip(evt, text) {
    if (!text) return hideTip();
    tooltip.classList.remove("help-tip", "stack-tip");
    tooltip.textContent = text;
    tooltip.style.display = "block";
    placeTooltip(evt.clientX, evt.clientY);
  }
  function showStackedTip(evt, headerText, items, totalText, noteText) {
    if (!items || !items.length) return hideTip();
    tooltip.classList.remove("help-tip");
    tooltip.classList.add("stack-tip");
    const rowNodes = items.map((item) => {
      const opSuffix = isFiniteMetric(item.opacity) && Number(item.opacity) < 1 ? `;opacity:${Number(item.opacity)}` : "";
      return el("div", { className: "stack-tip-row" }, [
        el("span", { className: "stack-tip-name" }, [
          item.color ? el("span", { className: "stack-tip-dot", style: `background:${item.color}${opSuffix}` }) : null,
          el("span", { textContent: item.label }),
        ]),
        el("span", { className: "stack-tip-val", textContent: item.valueText }),
      ]);
    });
    const noteLines = (Array.isArray(noteText) ? noteText : [noteText])
      .filter((line) => typeof line === "string" && line.trim())
      .map((line) => el("div", { className: "stack-tip-note", textContent: line.trim() }));
    tooltip.replaceChildren(
      el("div", { className: "stack-tip-head", textContent: headerText }),
      ...rowNodes,
      el("div", { className: "stack-tip-total" }, [
        el("span", { textContent: "Total" }),
        el("span", { textContent: totalText }),
      ]),
      ...noteLines
    );
    tooltip.style.display = "block";
    placeTooltip(evt.clientX, evt.clientY);
  }
  function showHelpTip(evtOrEl, title, text, formula) {
    if (!text && !formula) return hideTip();
    const formulaText = typeof formula === "string" ? formula.trim() : "";
    if (!text && !formulaText) return hideTip();
    tooltip.classList.remove("stack-tip");
    tooltip.classList.add("help-tip");
    const children = [
      el("div", { className: "help-tip-title", textContent: title || "Metric Help" }),
      text ? el("div", { className: "help-tip-body", textContent: text }) : null,
      formulaText
        ? el("div", { className: "help-tip-formula-wrap" }, [
            el("div", { className: "help-tip-formula-label", textContent: "Formula" }),
            el("div", { className: "help-tip-formula", textContent: formulaText }),
          ])
        : null,
    ].filter(Boolean);
    tooltip.replaceChildren(...children);
    tooltip.style.display = "block";
    if (evtOrEl && typeof evtOrEl.clientX === "number" && typeof evtOrEl.clientY === "number") {
      placeTooltip(evtOrEl.clientX, evtOrEl.clientY);
    } else if (evtOrEl && typeof evtOrEl.getBoundingClientRect === "function") {
      const r = evtOrEl.getBoundingClientRect();
      placeTooltip(r.left, r.bottom);
    }
  }
  function hideTip() {
    tooltip.style.display = "none";
    tooltip.classList.remove("help-tip", "stack-tip");
  }

  const HELP_TEXTS = {
    // Overview verdict, KPIs & sections
    "Automated Verdict": "Identifies the primary performance bottleneck using deterministic 8-rule forensics across client latency, queuing, KV cache, and routing telemetry. Follow the evidence links below to inspect the limiting subsystem.",
    "Dominant Limit": "Primary factor capping throughput or SLO attainment for this run, derived from client and server telemetry evidence. Address this limit first for the largest performance gain.",
    "SLO Attainment": "Percentage of requests that met all configured latency SLO targets (TTFT, TPOT, and E2E). Higher is better; drops below 95-99% indicate overload, queueing, or cache contention.",
    "Goodput": "Rate of completed requests per second (and per hour) that satisfied every latency SLO target. Higher goodput means more useful capacity delivered within quality bounds.",
    "Completed / SLO-Compliant Requests": "Count of completed requests versus SLO-compliant requests in the active analysis window. A wide gap between completed and SLO-compliant counts indicates latency SLO violations or failed requests.",
    "Request Throughput / Goodput": "Total completed request rate alongside SLO-compliant goodput (req/s) and output throughput (tok/s). Higher is better; goodput lagging throughput signals tail-latency degradation.",
    "TTFT (P50 / P90 / P99)": "Time to First Token measures how long clients wait from sending a request until receiving the first output token. Lower is better; high P90/P99 values point to queueing, long prompts, or low prefix cache hit rates.",
    "TPOT (P50 / P90 / P99)": "Time per Output Token (ms/tok) measures mean per-request decode latency per generated token after TTFT (distinct from per-step ITL). Lower is faster streaming; spikes indicate batch contention, memory-bandwidth saturation, or KV preemption.",
    "Error Rate": "Percentage of requests that failed with HTTP errors, timeouts, or connection drops during the window. Should stay near 0%; elevated rates indicate saturation or upstream routing failures.",
    "Peak KV Cache Usage": "Maximum HBM KV-cache usage observed across serving pods, paired with mean prefix cache hit rate. Approaching 90-100% triggers request queueing or KV preemption; higher prefix cache hit rate reduces prefill work.",
    "Accelerator Power (Avg / Peak)": "Mean and peak accelerator power draw (Watts) along with total energy consumed (Wh) and output tokens per Wh. Higher tokens/Wh indicates better serving energy efficiency.",
    "Warmup Phase (Auto 10%)": "Latency statistics (Min, P50, Mean, P90, P99, Max) during the initial 10% warmup window before caches and JIT kernels warm up. Much higher latency than steady state reveals warmup or cold-cache overhead.",
    "Warmup Phase": "Latency statistics (Min, P50, Mean, P90, P99, Max) measured during the configured warmup phase at the start of the run. Compare against steady state to verify warmup duration isolated startup effects.",
    "Steady-State Profiling Window": "Latency statistics (Min, P50, Mean, P90, P99, Max) after warmup completes, paired with the warmup-to-steady-state P50 ratio. Ratios well above 1.0x indicate significant warmup latency penalties relative to steady-state serving.",
    "Warmup vs Steady-State Latency": "Grouped bar comparison across all six canonical latency statistics (Min, P50, Mean, P90, P99, Max) between the initial warmup phase and steady-state serving for TTFT, TPOT, and E2E.",
    "o-verdict": "Summarizes the automated root-cause verdict, overall SLO attainment, and SLO-compliant goodput for the simulation run. Click any evidence bullet to jump directly to the supporting diagnostic page.",
    "o-kpis": "Top-level client throughput, latency percentiles, reliability, KV cache pressure, and energy efficiency for the selected analysis window. Toggle between Profiling Steady-State and Full Run in the top bar.",
    "o-thr": "Tracks completed req/s, SLO-compliant goodput (req/s), and output throughput (tok/s) over the run timeline. Divergence between Completed req/s and Goodput req/s highlights periods of SLO violation.",
    "o-perf": "Plots selectable latency statistics (Min, P50, Mean, P90, P99, Max) over time with a P50-to-P90 shaded band when both P50 and P90 are active, relative to the SLO threshold. Wide bands or crossings above the SLO line indicate tail-latency instability.",
    "o-conc": "Compares client-side in-flight requests against serving engine running requests over time. Client in-flight exceeding engine running requests indicates Inference Gateway/EPP buffering or an engine waiting queue.",
    "o-cold": "Compares early warmup or initial 10% latency against steady-state latency across Min, P50, Mean, P90, P99, and Max for TTFT, TPOT, and E2E. High warmup-to-steady-state ratios suggest prefix caches or engine state needed time to stabilize.",
    "o-power": "Compares mean versus peak accelerator power draw (Watts) across each serving pod. Large gaps or near-TDP peaks highlight compute-intensive bursts or uneven pod load.",

    // Bottlenecks page
    "b-verdict": "Lists prioritized diagnostic findings ranked by severity along with supporting metrics and tuning recommendations. Address critical findings first, as they represent the primary throughput or latency ceiling.",
    "b-timeline": "Tracks each serving engine role's quantitative saturation over time—running and waiting requests, KV cache usage (%) against the 90% threshold, and time-in-state distribution (Healthy, Queueing, KV Pressure, Evicting, Error). Hover any interval to inspect exact counts, cache hit rate, preemptions, and failures.",
    "b-stage-breakdown": "End-to-end request stage latency breakdown over time (EPP Scheduling, Queue Time, optional KV Transfer (NIXL), Prefill Time, Decode Time) with P50/P90 toggle, E2E and TTFT share attribution, dominant stage badge, and peak-window shift detection.",
    "Stage Share Summary": "Per-stage duration (ms), share of E2E total (Stage / Total Stage Time), and share of TTFT (Stage / TTFT Stage Time) for the selected slice.",
    "EPP Scheduling": "Time spent in Endpoint Picker (EPP) routing and target-pod selection before request dispatch.",
    "Queue Time": "Time spent waiting in the engine scheduler queue before prompt prefill or decode execution begins (excluding NIXL KV transfer wait).",
    "KV Transfer (NIXL)": "Disaggregated Prefill-to-Decode KV cache transfer duration over NIXL while waiting for remote KVs.",
    "Prefill Time": "Time spent executing prompt prefill computation to populate initial KV state.",
    "Decode Time": "Time spent generating output tokens during the decode phase. In client-only fallback mode, estimated as max(0, Client E2E - Client TTFT).",
    "Decode First-Step Compute": "Decode pod's local first-step forward pass after receiving transferred KV cache from the Prefill pod (reconciled within server TTFT).",
    "Prefill Engine / API Overhead": "Internal Prefill pod overhead beyond Queue and Prefill compute: API tokenization, chat template formatting, engine IPC, scheduler overhead, and KV handshake.",
    "Decode Handoff / SSE Overhead": "Decode pod handoff and first-token overhead: P-to-D routing sidecar handoff, Decode scheduler wake-up after KV arrival, first-token sampling, and SSE flush.",
    "Engine / API Overhead": "Internal serving engine and API server overhead inside server TTFT beyond measured Queue, KV Transfer, and Prefill stages (tokenization, chat template, engine IPC, scheduling, and first-token SSE flush).",
    "Server TTFT Residual": "Server-side TTFT residual inside engine TTFT beyond measured Queue, KV Transfer, and Prefill stages (in P/D disaggregation, decomposed into Decode First-Step Compute + Prefill Engine / API Overhead + Decode Handoff / SSE Overhead in TTFT Stage Decomposition).",
    "Client / Gateway / Network": "Residual client, network, and Inference Gateway latency beyond EPP Scheduling and engine TTFT.",
    "Server & Gateway Residual": "Combined EPP Scheduling, Server TTFT Residual, and Client / Gateway / Network latency inside TTFT (hover bar or inspect summary cards for granular breakdown).",
    "Client / Gateway / Routing": "Combined EPP Scheduling and Client / Gateway / Network latency inside TTFT (hover bar or inspect summary cards for granular breakdown).",
    "P/D Engine TTFT (Queue + Prefill)": "Combined Prefill and Decode engine TTFT (queue wait, prompt prefill, and residual) when legacy telemetry pooled disaggregated stage histograms.",
    "Gateway / EPP Overhead": "Residual client, network, and Inference Gateway overhead inside TTFT after subtracting EPP Scheduling, Queue Time, and Prefill Time.",
    "Client TTFT (Unattributed)": "Observed client Time to First Token when engine stage histograms are unavailable to split EPP Scheduling, Queue Time, Prefill Time, and residual overhead.",
    "Unattributed TTFT": "Observed client TTFT when stage histograms are unavailable or do not expose that statistic (such as single-request Min or legacy runs without server mean stage histograms).",
    "Decode Time (Est.)": "Estimated Decode Time = max(0, Client E2E - Client TTFT) when engine stage histograms are unavailable.",
    "b-ttft-decomp": "Decomposes client Min, P50, Mean, P90, P99, and Max TTFT into serving engine prefill and decode first-step compute, queue wait, optional NIXL KV transfer, engine/API and handoff overhead, EPP scheduling, client/gateway/network latency, and unattributed client TTFT. The tallest bar segment identifies where to focus optimization.",
    "b-cache-turns": "Shows the share of prompt tokens served from prefix cache versus prefill compute, plus multi-turn follow-up latency stats. Higher prefix cache share lowers prefill compute and speeds up multi-turn chats.",
    "b-routing": "Compares the EPP predicted prefix cache hit rate against actual serving engine prefix cache hit rate over time. A large positive gap means EPP expects cache affinity that pods are evicting or missing.",
    "b-pods": "Measures load balance across serving pods via max-to-mean utilization skew and per-pod concurrency, waiting requests, KV cache usage, and prefix cache hit rate. High skew ratios (>1.3x) indicate hot-spotting or suboptimal routing.",
    "Load skew ratio (max / mean)": "Ratio of the busiest pod's load to the mean pod load across the serving pool. Values near 1.0x show balanced routing; high values indicate uneven pod utilization.",
    "Pod": "Kubernetes pod or engine instance identifier in the serving pool.",
    "Role": "Serving role assigned to this pod (worker, prefill, decode, or epp).",
    "Running": "Mean concurrent running requests on this pod. Should be balanced across pods with identical hardware; consistently higher values on one pod indicate routing imbalance.",
    "Waiting": "Mean or peak requests queued waiting for KV blocks or scheduler slots on this pod. Sustained values above zero signal pod-level saturation.",
    "Peak KV Cache (%)": "Highest HBM KV-cache usage percentage reached on this pod. Values near 90-100% risk preemption and queueing.",
    "Prefix Cache Hit Rate (%)": "Percentage of prompt tokens hitting this pod's local prefix cache. Higher values reflect stronger cache affinity and reduced prefill compute.",

    // Serving page
    "s-flow": "Visualizes request flow across EPP and engine stages, either stepped through time (Replay) or averaged across the run (Summary). Use the scrubber to spot when queues or KV pressure build up.",
    "Endpoint Picker (EPP)": "Endpoint Picker (EPP) routing layer forwarding requests to serving pods. Shows request rate and currently tracked in-flight requests.",
    "Workers": "Aggregated model-serving worker pool executing both prefill and decode. Shows request rate, token rates, running/waiting counts, and KV cache usage.",
    "Prefill": "Disaggregated prefill workers processing input prompts and populating KV cache. High waiting counts or KV cache usage here indicate prompt-processing saturation.",
    "Decode": "Disaggregated decode workers generating output tokens from transferred KV state. High running counts or KV cache usage here limit decode concurrency.",

    "s-latency": "Server-side latency histograms and stage breakdowns directly from serving engine and EPP metrics. Compare against client latency to separate engine execution from network and Inference Gateway overhead.",
    "Server TTFT (P50)": "Engine-measured median (with P90/P99) time to first token inside the serving engine. Lower is better; gaps versus client TTFT reflect Inference Gateway, EPP, and network latency.",
    "Server TPOT (P50)": "Engine-measured median (with P90/P99) time per output token (ms/tok) during decode; falls back to engine Inter-Token Latency (ITL) when the TPOT histogram is absent. Lower values mean faster per-token generation under load.",
    "Server E2E (P50)": "Engine-measured median (with P90/P99) total request execution time from admission to final token. Tracks overall server-side serving latency.",
    "Stage P50 (Queue / Prefill / Decode)": "Median time spent in engine scheduler queue (Queue), prompt prefill (Prefill), and token decode (Decode). Identifies which execution phase dominates request latency.",
    "Stage P50 (KV Transfer / P/D TTFT / Decode Est.)": "Median P/D stage attribution derived from measured NIXL KV transfer and client TTFT/E2E split when legacy telemetry pooled P/D engine histograms.",
    "Server Latency Percentiles over Time": "Plots server-reported P50 latency (solid line) and P50-to-P90 spread (shaded band) for TTFT, TPOT, or E2E. Rising bands indicate growing engine contention.",
    "Request Stage Latency Breakdown": "Stacked area chart of P50 or P90 EPP Scheduling, Engine Queue Time, optional KV Transfer (NIXL), Prefill Time, and Decode Time over elapsed time (or P/D pooled / client fallback when role histograms are unavailable). Total stacked height shows combined stage duration and each layer reveals which stage dominates.",
    "Prompt Token Throughput: Prefix Cache vs Prefill Compute": "Stacked area chart of prompt tokens/s satisfied from prefix cache versus prefill compute on accelerators. Total height equals total prompt token processing rate; higher prefix cache share reduces prefill compute.",

    "s-engine": "Serving engine concurrency, waiting requests, prefill and decode token throughput, decode speed, and per-pod serving statistics. Use this section to inspect engine saturation and pod balance.",
    "Running Requests": "Mean and peak number of requests concurrently executing inside serving engine batches. Higher values show greater batch utilization up to KV or compute limits.",
    "Waiting Requests": "Mean and peak requests queued in the serving engine waiting for compute slots or KV cache blocks. Sustained non-zero values mean the engine is saturated.",
    "Prefill Throughput": "Mean and peak rate of input prompt tokens prefill-processed per second across serving pods. Higher values indicate faster prefill capacity or prefix cache reuse.",
    "Decode Throughput": "Mean and peak rate of output tokens decoded (generated) per second across the pool. Higher values measure total decode serving capacity.",
    "Running vs Waiting Requests over Time": "Tracks active running and queued waiting requests per engine role (or aggregated serving engine vs EPP in-flight) over time. Rising waiting curves signal overload.",
    "Prefill vs Decode Token Throughput": "Plots input prefill tokens/s alongside output decode tokens/s across the run. Drops during steady load often indicate queueing or preemptions.",
    "vLLM TPOT over Time": "Tracks server-side median (P50) and tail (P90) time per output token (ms/tok) across the run; falls back to engine Inter-Token Latency (ITL) when the TPOT histogram is absent. Upward drift signals batch-size contention or memory bandwidth limits.",
    "Prompt Token Breakdown": "Total sent prompt tokens split into local prefix cache, shared-prefix tokens recomputed despite prior turns, and uncached prompt tokens; the vertical marker indicates the theoretical hit ceiling with an infinite cache and ideal routing.",
    "Prompt Tokens per Request": "Average prompt tokens per request split between prefix cache and prefill compute tokens, alongside P50 and P90 request input sequence lengths.",
    "Prompt Token Sources by Role": "Total prompt tokens processed per serving role broken down by engine source: local HBM prefix cache, external/CPU KV transfer, and prefill compute.",
    "Req/s": "Mean completed request rate handled by this pod. Compare across pods to verify balanced traffic distribution.",
    "Prefill (tok/s)": "Mean input prompt token prefill processing rate on this pod. Higher values reflect heavier prompt load or larger input sequence lengths.",
    "Decode (tok/s)": "Mean output token decode generation rate on this pod. Measures per-pod decode throughput contribution.",
    "KV Cache (%)": "Peak or mean HBM KV-cache usage on this pod. High values indicate memory pressure that can throttle batch size.",

    "s-load": "Inference Gateway and Endpoint Picker (EPP) request load, scheduling overhead, cache prediction, and destination distribution. Helps isolate routing bottlenecks from engine bottlenecks.",
    "EPP In-Flight Requests": "Mean and peak requests actively tracked by EPP, plus ready backend endpoints. Compare against engine running requests to spot routing backlog.",
    "EPP Scheduling Latency": "Median and P90 time spent by EPP selecting a target pod for each request. Should stay in low milliseconds; spikes point to EPP CPU or scoring overhead.",
    "EPP Plugin Latency": "Median and P90 execution time of EPP routing plugins, alongside Inference Gateway P90 latency. High values indicate expensive scoring plugins or gateway congestion.",
    "P/D Routing Ratio": "Percentage of requests routed through disaggregated Prefill/Decode paths and the resulting avoided prefill ratio. Confirms P/D disaggregation is actively routing traffic.",
    "EPP Predicted Prefix Cache Hit Rate": "EPP-estimated prefix cache hit rate based on EPP's prefix index. Compare with actual serving engine prefix cache hit rate to evaluate cache-aware routing accuracy.",
    "EPP Load · In-Flight & Request Rate": "Compares EPP in-flight concurrency against EPP request rate and serving engine request rate over time. Divergence reveals queueing or dropped requests between EPP and serving pods.",
    "EPP Scheduling & Plugin Latency": "Plots P50 and P90 latency for EPP scheduling and plugin evaluation over time. Flat, low lines confirm EPP is not adding tail latency.",
    "Destination Endpoint": "Target backend pod or endpoint receiving routed requests.",
    "Share %": "Percentage of total routed traffic sent to this endpoint. Large disparities indicate uneven routing or sticky prefix affinity.",
    "Rate (req/s)": "Mean request rate forwarded to this endpoint.",
    "In-Flight Token Load": "Mean active token load tracked in-flight on this endpoint by EPP. Used by load-aware scorers to avoid overloaded pods.",

    "s-cache": "High-Bandwidth Memory (HBM) KV-cache capacity, block geometry, and usage over time. Exhausting HBM blocks forces request waiting or KV eviction.",
    "KV Cache Usage (Peak / Mean)": "Peak and average percentage of HBM KV-cache blocks used during the run. Values above 90% signal severe memory pressure.",
    "HBM Capacity": "Total token capacity available in accelerator HBM for KV caching across the configured block pool. Larger capacity supports higher concurrency and longer contexts.",
    "Effective Capacity": "Usable KV-cache token capacity and maximum theoretical concurrency at target sequence lengths. Higher values allow more concurrent sessions before queueing.",
    "Block Size & Device KV Blocks": "Tokens stored per KV cache block and total allocated accelerator blocks per engine. Determines memory granularity and total KV pool size.",
    "HBM KV Cache Usage over Time": "Plots HBM KV-cache usage % over time against the 90% pressure threshold line. Sustained readings near or above the red line trigger preemption.",
    "KV Cache Tier Share (HBM · Host Memory · NVMe)": "Proportional distribution (%) of active cached KV state across Accelerator HBM, Host Memory (CPU RAM offload), and NVMe (local disk offload) over time.",
    "Engine": "Engine worker index within the pod.",
    "Block Size": "Number of tokens stored per paged KV-cache block.",
    "Device KV Blocks": "Total paged KV-cache blocks allocated in accelerator memory.",
    "KV Tokens": "Total token slots backed by the allocated accelerator KV blocks.",
    "Max Concurrency": "Maximum concurrent requests sustainable in KV cache at the reference context length.",

    "s-kvreuse": "Measures prefix cache effectiveness and breaks down prompt tokens by local prefix cache, external KV transfer, or prefill compute. Higher reuse cuts TTFT and saves prefill compute.",
    "Prefix Cache Hit Rate": "Mean and peak percentage of prompt tokens served from the serving engine's automatic prefix cache. Higher values reduce prefill latency and accelerator compute.",
    "External Prefix Cache Hit Rate": "Mean percentage of prompt tokens served from an external or offloaded KV tier, plus avoided prefill ratio. Zero when external KV sharing is inactive.",
    "Decode External KV Share (NIXL)": "Mean percentage of Decode-pool prompt tokens fulfilled via disaggregated external KV transfer (NIXL). Tracked separately from Prefill local prefix cache hits to avoid double-counting.",
    "Local Prefix Cache Throughput": "Rate of prompt tokens served from local HBM prefix cache (tok/s) and external KV sources. Higher rates mean less redundant prefill computation.",
    "Prefill Compute Throughput": "Rate of prompt tokens requiring prefill computation (tok/s) and recomputed tokens after eviction. Lower recomputed rates mean better cache retention.",
    "Prefix Cache Hit Rate over Time": "0–100% stacked area chart splitting prompt tokens into Local Prefix Cache (%), External KV Transfer (%), Recomputed Prefix (%) (prior-turn shared prefixes missed and recomputed), and Uncached Prompt (%) (first-time unseen prompt tokens).",
    "Prompt Token Sources over Time": "Stacked area chart of prompt tokens/s served from local prefix cache, external KV transfer, and prefill compute over time. Total height reflects total prompt throughput; larger prefix cache layers indicate strong KV reuse.",

    "s-offload": "Tracks multi-tier KV cache usage across VRAM, CPU host memory, and NVMe storage alongside offload, restore, and disaggregated NIXL KV transfer throughput.",
    "VRAM KV Cache Usage": "Mean and peak percentage of allocated accelerator VRAM KV-cache blocks currently in use across serving pods.",
    "CPU KV Cache Usage": "Mean and peak percentage of CPU host-memory KV offload capacity currently in use. High usage indicates active KV spilling from accelerator memory.",
    "NVMe KV Cache Usage": "Mean and peak percentage of NVMe/disk KV offload capacity currently in use across serving pods.",
    "KV Offload Throughput": "Total bytes (or mean/peak MiB/s) offloaded from accelerator memory to secondary host or NVMe tiers. Non-zero values confirm KV blocks are spilling.",
    "KV Restore Throughput": "Total bytes (or mean/peak MiB/s) restored from secondary offload tiers back to accelerator memory for reuse. High restore throughput can add latency if host-to-device bandwidth saturates.",
    "NIXL KV Transfer Throughput": "Mean NIXL KV-transfer bandwidth (MiB/s), P95 transfer latency, and failed transfer count in P/D Disaggregation mode. Lower latency and zero failures indicate healthy KV transfer.",
    "KV Offload Lookup Sync Delay (P95)": "95th-percentile lookup synchronization delay (ms) when checking offloaded KV cache entries. Lower values keep cache lookup overhead minimal.",
    "KV Cache Usage by Tier": "Plots VRAM, CPU host-memory, and NVMe KV cache usage (%) over time on a shared 0–100% axis.",
    "KV Offload, Restore & NIXL Transfer Throughput": "Plots KV offload, restore, and NIXL KV transfer throughput (MiB/s) over time.",
    "KV Offload & Restore Throughput": "Plots KV offload and restore throughput (MiB/s) over time.",

    "s-evict": "Monitors accelerator KV-cache block churn, block residency lifetimes, and engine request preemptions. High eviction rates or short reuse gaps indicate thrashing, while preemptions mean active requests lost KV blocks and had to wait or recompute.",
    "KV Eviction · Block Churn, Residency & Preemptions": "Monitors accelerator KV-cache block churn, block residency lifetimes, and engine request preemptions. High eviction rates or short reuse gaps indicate thrashing, while preemptions mean active requests lost KV blocks and had to wait or recompute.",
    "Total Preemptions": "Cumulative KV-cache preemption events observed across the run alongside peak KV cache usage. Zero is healthy; any preemptions degrade tail TPOT and TTFT.",
    "Peak Preemption Rate": "Highest observed rate of preemptions per second. Spikes correspond directly to HBM exhaustion and latency jitter.",
    "KV Cache Pressure": "Indicates whether KV pressure was detected based on >=90% HBM usage or >0 preemptions. Healthy means no KV-induced eviction occurred.",
    "KV Block Eviction Rate": "Peak and mean rate of accelerator KV-cache blocks evicted per minute (scaled by 1 / Sampling_Fraction when sampled), plus total evicted blocks across the run. High churn signals cache pressure or short retention.",
    "KV Block Idle Time Before Eviction (P50 / P90)": "Median and 90th-percentile idle duration (seconds) a cached KV block sat unused before eviction from the engine's KV block idle-before-evict histogram. Very short idle times indicate aggressive thrashing.",
    "KV Block Lifetime (P50 / P90)": "Median and 90th-percentile total duration (seconds) a KV block remained allocated from creation to eviction from the engine's KV block lifetime histogram. Short lifetimes reduce multi-turn prefix cache reuse.",
    "KV Block Reuse Gap (P50 / P90)": "Median and 90th-percentile time elapsed (seconds) between consecutive prefix-cache hits on the same KV block from the engine's KV block reuse-gap histogram. Helps size KV capacity to match conversation turn intervals.",
    "Request Preemption Rate over Time": "Plots serving engine preemption events per second across the simulation timeline. Non-zero spikes mark moments of acute KV memory exhaustion.",
    "KV Block Eviction Rate (blocks/min)": "Tracks physical accelerator KV-cache blocks evicted per minute over time (scaled by 1 / Sampling_Fraction when KV metric sampling is configured). Spikes reveal intervals of intense KV-cache turnover.",
    "KV Block Idle Time Before Eviction (s)": "Plots P50 and P90 idle duration (seconds) before cached KV blocks are evicted. Falling idle times show newly populated blocks being evicted soon after last use.",
    "KV Block Lifetime (s)": "Plots P50 and P90 total residency lifetime (seconds) for allocated KV blocks. Longer allocation lifetimes give multi-turn and shared-prefix traffic more time to reuse cached state.",
    "KV Block Reuse Gap (s)": "Plots P50 and P90 time gap (seconds) between consecutive cache reuses of a KV block. Compare against block lifetime to verify blocks survive long enough for repeat turns.",

    "s-power": "Hardware-profile-driven telemetry for accelerator power draw, compute/memory utilization, HBM usage, and thermal state. Expand the drawer below for device and metric-source mappings.",
    "Accelerator Power": "Mean and peak accelerator power (W) and total energy consumed (Wh). Sustained peaks near TDP indicate heavy compute or memory saturation.",
    "Accelerator Utilization": "Mean and peak compute utilization (%) of the serving accelerators. Very low utilization during high latency suggests queueing, host, or network bottlenecks.",
    "Accelerator Memory": "Used versus total accelerator device memory (HBM) and memory bandwidth utilization %. High memory usage leaves less headroom for KV cache expansion.",
    "Accelerator Temperature": "Mean and peak device temperature (C) alongside energy efficiency (output tokens/Wh). Elevated temperatures risk thermal throttling.",
    "Accelerator Power Draw over Time": "Plots accelerator power draw (W) over the run timeline. Drops during steady load indicate stalled pipelines or batch starvation.",
    "Accelerator Compute & Memory Bandwidth Utilization": "Tracks compute utilization % alongside memory bandwidth utilization % over time. High memory bandwidth % reflects decode-bound execution; high compute % reflects prefill.",
    "Node": "Kubernetes worker node hosting target serving pods or accelerator devices.",
    "PCI BDF / Device": "Hardware PCI bus/device/function address or device identifier allocated to the pod.",
    "Metric": "Telemetry or latency metric name.",
    "Telemetry Source": "Exporter or Prometheus metric family supplying this measurement.",

    "s-grafana": "Pre-scoped deep links into cluster Grafana dashboards locked to this simulation's exact start and end timestamps.",

    // Requests page
    "r-fidelity": "Compares intended trace arrival rate against actual client send rate and 90th-percentile schedule drift. Drift spikes mean the load generator fell behind the target replay schedule.",
    "r-drift-hist": "Distribution of requests binned by schedule drift magnitude (ms). Tight clustering near 0 ms confirms accurate open-loop trace timing.",
    "r-lanes": "Each horizontal row (S1..Sn) is a concurrent execution slot reused as soon as its prior request finishes, colored by prompt prefix-cache hit tier (cold <10%, partial 10–70%, warm ≥70%). Hover a bar to inspect slot, session, turn, timing, cache hit %, tokens, and TTFT.",
    "Sessions over Time": "Each horizontal row (S1..Sn) is a concurrent execution slot reused as soon as its prior request finishes, colored by prompt prefix-cache hit tier (cold <10%, partial 10–70%, warm ≥70%). Hover a bar to inspect slot, session, turn, timing, cache hit %, tokens, and TTFT.",
    "r-buckets": "Groups request latency by input sequence length and prefix cache hit bucket, plus per-request time-vs-length latency scatter plots. Reveals how long input sequences, cache misses, and temporal hotspots amplify tail latency.",
    "TTFT by Input Sequence Length Bucket": "Median (P50) and tail (P90) TTFT across input sequence length ranges. Rising bars confirm prefill compute scaling with input sequence length.",
    "TTFT by Prefix Cache Hit Bucket": "Median (P50) and tail (P90) TTFT grouped by prefix cache hit rate bucket. Lower bars in high-hit buckets quantify the speedup from KV reuse.",
    "TTFT by Time and Input Sequence Length": "Per-request scatter plot of TTFT (ms) across elapsed run time (x-axis) and input sequence length (y-axis). Each dot is one request colored by its TTFT; warmer colors highlight when and at what input lengths latency degraded.",
    "TPOT by Time and Output Sequence Length": "Per-request scatter plot of TPOT (ms/tok) across elapsed run time (x-axis) and output sequence length (y-axis). Each dot is one request colored by its TPOT; warmer colors show decode slowdowns under longer contexts or contention.",
    "r-conv": "Summarizes multi-turn session structure by turn count and conversation tree depth. Deeper or multi-turn sessions rely heavily on prefix cache persistence across turns.",
    "Turns per Session": "Histogram of session counts grouped by number of user turns. Multi-turn workloads benefit most from sticky cache-aware routing.",
    "Conversation Tree Depth": "Histogram of sessions by branching conversation tree depth. Greater depth indicates branching or long-horizon agentic workflows.",
    "r-cdf": "Cumulative Distribution Function (CDF) curves and full statistical percentiles for TTFT, TPOT, E2E, and schedule drift. Steep curves left of the red SLO line indicate strong compliance.",
    "TTFT (ms)": "Cumulative distribution of Time to First Token across all requests in the active window. Values to the left of the dashed red line meet the TTFT SLO.",
    "TPOT (ms/tok)": "Cumulative distribution of Time per Output Token (ms/tok) across all requests. Steep vertical curves indicate consistent token streaming speed.",
    "E2E (ms)": "Cumulative distribution of End-to-End request completion latency. Combines queueing, TTFT, and total decode time.",
    "Schedule Drift (ms)": "Cumulative distribution of client launch delay relative to the trace target schedule. Values near 0 ms confirm accurate replay pacing.",
    "TTFT": "Time to First Token from request dispatch until the first response token arrives. Lower is better.",
    "TPOT": "Mean per-request Time per Output Token (ms/tok) during decode after TTFT (distinct from per-step ITL). Lower values mean faster token streaming.",
    "E2E": "Total End-to-End latency from request send until final token completion. Lower is better.",
    "Schedule Drift": "Schedule drift: delay between scheduled trace arrival time and actual client dispatch (ms). Values near 0 ms indicate accurate replay.",
    "Avg": "Arithmetic mean across requests in the active window (sensitive to extreme outliers).",
    "Min": "Fastest single request observation recorded in the window.",
    "P50": "Median (50th percentile): half of requests were faster than this value.",
    "P90": "90th percentile: 90% of requests finished within this value.",
    "P95": "95th percentile tail latency boundary.",
    "P99": "99th percentile worst-case tail latency excluding the top 1%.",
    "Max": "Slowest single request observation recorded in the window.",
    "r-errors": "Categorizes failed requests by status code or error type and displays up to 200 redacted failure records. Filter by status or category pills to inspect specific failure modes.",
    "Req #": "Sequential request index assigned by the benchmark runner.",
    "Time (s)": "Request start timestamp in seconds relative to run start.",
    "Category": "Normalized error classification (e.g., timeout, http_5xx, connection).",
    "Code": "HTTP status code or transport error code returned for the request.",
    "Latency": "Elapsed time until the request failed or timed out.",
    "Error Message (Redacted)": "Sanitized error summary with credentials and prompt/response payloads stripped.",

    // System page
    "y-kpis": "Host-level CPU, memory, disk, network, and node power telemetry from node_exporter for nodes running target pods. Use the scope selector to inspect all nodes in aggregate or drill down into an individual node.",
    "Host CPU %": "Mean and peak host CPU utilization across target worker nodes. Sustained >85% CPU can starve tokenization, HTTP handling, or CPU KV offloading.",
    "Host Memory Avail": "Mean available host RAM versus total node memory. Low headroom risks OS page-cache thrashing or pod OOMKills.",
    "Disk Available": "Available filesystem capacity on target nodes alongside mean disk read/write throughput (scaled across B/s–PiB/s). Low disk or heavy I/O can stall model loading.",
    "Host Power & Energy": "Mean and peak whole-node power draw (W) and cumulative host energy (Wh). Includes CPU, memory, fans, and accelerators when exposed by the node exporter.",
    "Host CPU Utilization": "Mean and peak host CPU utilization (%) from node_exporter. Sustained >80-85% CPU can bottleneck tokenization, HTTP handling, or CPU KV offloading.",
    "Host Memory Usage": "Mean used versus total host system RAM (scaled across B–PiB) alongside peak usage % and minimum available RAM. Tight headroom risks OS page-cache thrashing or pod OOMKills.",
    "Disk I/O & Capacity": "Mean host block-device read and write throughput (scaled across B/s–PiB/s) alongside minimum free and total filesystem capacity (scaled across B–PiB). Heavy I/O or low free disk can stall model loading.",
    "Network Throughput": "Mean and peak host network receive (RX) and transmit (TX) throughput (scaled across B/s–PiB/s) across target nodes. Elevated rates reflect heavy API traffic or disaggregated KV transfers.",
    "Host Power": "Mean and peak whole-node power draw (W) and cumulative host energy (Wh). Includes CPU, memory, fans, and accelerators when exposed by host power exporters.",
    "Accelerator Power & Thermal": "Mean and peak serving accelerator power draw (W) and peak device temperature (°C) for comparison against total host node power.",
    "y-nodes": "Per-node summary of serving roles, pod counts, CPU, memory, disk I/O, free disk, network RX/TX, host power, and accelerator telemetry. Click any row or scope pill to focus charts on that node.",
    "Per-Node Summary": "Per-node summary of serving roles, pod counts, CPU, memory, disk I/O, free disk, network RX/TX, host power, and accelerator telemetry. Click any row or scope pill to focus charts on that node.",
    "Roles & Pods": "Serving roles (e.g., worker, prefill, decode, epp) and count of target serving pods scheduled on this node.",
    "CPU (Mean / Peak)": "Mean and peak host CPU utilization (%) observed on this node.",
    "Memory (Used / Total · Peak %)": "Mean used host RAM versus total physical node RAM, paired with peak memory usage %.",
    "Disk I/O (R / W)": "Mean block-device read and write throughput (scaled across B/s–PiB/s) observed on this node.",
    "Free Disk": "Available filesystem capacity versus total filesystem size on this node.",
    "Network (RX / TX)": "Mean host network receive (RX) and transmit (TX) throughput (scaled across B/s–PiB/s) on this node.",
    "Host Power (Mean / Peak)": "Mean and peak whole-node power draw (Watts) measured on this node.",
    "Accelerator (Count · Util · Power · Temp)": "Allocated accelerator count, mean compute utilization (%), mean power draw (W), and peak device temperature (°C) on this node.",
    "y-cpu": "Time series of host CPU utilization % on nodes hosting target serving pods (with per-node lines overlaid in aggregate multi-node runs). Spikes coinciding with tail latency point to host CPU contention.",
    "Charts Node Scope": "Selects whether the four host-system time-series charts display cluster-wide aggregate telemetry across all target nodes or isolate a single serving node. Synchronized with the top scope selector and Per-Node Summary table.",
    "y-mem-disk": "Tracks host memory used and available (left axis, adaptive B–PiB) alongside disk read/write throughput (right axis, adaptive B/s–PiB/s) over time. Sharp memory changes indicate host-RAM allocation or KV spilling.",
    "Host Memory & Disk I/O": "Tracks host memory used and available (left axis, adaptive B–PiB) alongside disk read/write throughput (right axis, adaptive B/s–PiB/s) over time. Sharp memory changes indicate host-RAM allocation or KV spilling.",
    "y-net": "Time series of host network receive (RX) and transmit (TX) throughput (adaptive B/s–PiB/s) over elapsed run time, with optional per-interface breakdown across physical and fabric NICs. Spikes highlight ingress/egress bursts or disaggregated KV-transfer pressure.",
    "Network RX / TX Throughput": "Time series of host network receive (RX) and transmit (TX) throughput (adaptive B/s–PiB/s) over elapsed run time, with optional per-interface breakdown across physical and fabric NICs. Spikes highlight ingress/egress bursts or disaggregated KV-transfer pressure.",
    "Network Interface": "Filters the Network RX / TX Throughput chart to aggregate traffic across all physical and fabric interfaces, compare active interfaces side by side (By NIC), or inspect a single interface. Virtual and container interfaces are excluded.",
    "y-power": "Compares total host node power against accelerator-only power draw (W) over time. The gap represents CPU, memory, networking, and chassis overhead.",
    "Host & Accelerator Power": "Compares total host node power against accelerator-only power draw (W) over time. The gap represents CPU, memory, networking, and chassis overhead.",

    // Config & Logs page
    "c-manifest": "Full simulation configuration, target endpoint metadata, trace parameters, and workload settings with secrets redacted. Use this JSON snapshot to reproduce or audit the run.",
    "c-logs": "Redacted task lifecycle events and runner stdout/stderr output. Filter by keyword or toggle Errors / Warnings Only to diagnose runner or setup issues.",

    // Workload page
    "w-config": "Configured dataset, trace format, workload scenario, source trace replay slice, duration/warmup windows, pacing mode, concurrency limit, and time scale factor for this run.",
    "Dataset": "Trace dataset or prompt corpus replayed during this run, along with the input trace format and workload scenario category.",
    "Replay Time Range": "Start and end timestamps (in source trace seconds) sliced from the input trace, derived from configured bounds or replay duration multiplied by scale factor.",
    "Replay Duration & Warmup": "Configured active replay duration alongside the initial warmup window excluded from steady-state profiling KPIs and optional post-send drain grace period.",
    "Pacing Mode": "Whether request dispatch follows original source trace timestamps (Fixed Schedule) or maintains a bounded pool of simultaneous active slots (Concurrency).",
    "Configured Concurrency": "Maximum simultaneous in-flight request or session slots configured for concurrency-paced runs, or trace-driven arrival pacing for fixed-schedule runs.",
    "Scale Factor": "Time-compression multiplier applied to source trace inter-arrival intervals (>1x accelerates arrival rate; <1x slows arrival rate).",
    "w-kpis": "Window-scoped workload profile summarizing request counts, token totals, ISL/OSL distributions, decode intensity regimes, event-sweep concurrency, and arrival burstiness.",
    "Requests & Tokens": "Total, completed, and failed requests in the active window alongside total input (ISL) and output (OSL) tokens.",
    "Input Sequence Length (ISL P50)": "Median input sequence length in input tokens, with mean, tail percentiles (P90, P99), and coefficient of variation (CV). High CV indicates wide input sequence length heterogeneity.",
    "Output Sequence Length (OSL P50)": "Median output sequence length in generated tokens, with mean, tail percentiles (P90, P99), and coefficient of variation (CV).",
    "OSL / ISL Ratio (P50)": "Median per-request ratio of generated output tokens to input tokens. Values below 0.25 are prefill-heavy; values above 1.0 are decode-heavy.",
    "In-Flight Concurrency (Mean)": "Time-weighted mean of simultaneously active requests from exact request arrival-to-completion intervals, with time-weighted P50/P90/P99 and exact peak concurrency.",
    "Mean Arrival Rate": "Average request arrival rate over the active window alongside peak bucket arrival rate, peak-to-mean burstiness, and inter-arrival CV.",
    "Workload Regime Mix (OSL / ISL Ratio)": "Share of window requests classified as Prefill-Heavy (OSL/ISL < 0.25), Balanced (0.25–1.0), or Decode-Heavy (OSL/ISL > 1.0).",
    "w-isl": "Input sequence length (input token) distribution across adaptive bins, empirical CDF, and 6-stat summary (Min, P50, Mean, P90, P99, Max).",
    "Request ISL Distribution": "Histogram of request counts or percentage share across adaptive input-token bins in the active analysis window.",
    "Request ISL CDF": "Empirical cumulative distribution function of input sequence lengths (tokens), showing the fraction of requests at or below each input sequence length.",
    "w-osl": "Output sequence length (generated token) distribution across adaptive bins, empirical CDF, and 6-stat summary (Min, P50, Mean, P90, P99, Max).",
    "Request OSL Distribution": "Histogram of request counts or percentage share across adaptive output-token bins in the active analysis window.",
    "Request OSL CDF": "Empirical cumulative distribution function of output sequence lengths (tokens), showing the fraction of requests at or below each generation length.",
    "w-joint": "Per-request input vs output sequence length scatter colored by decode intensity regime, alongside bucketed output sequence length and OSL/ISL ratio profiles.",
    "Input Sequence Length vs Output Sequence Length": "Per-request scatter plot of Input Sequence Length (ISL) versus Output Sequence Length (OSL), colored by decode intensity regime with a dashed OSL = ISL (1:1) reference line. Hover any point to inspect request tokens, ratio, latency, and status.",
    "OSL by Input Sequence Length Bucket": "Grouped comparison of OSL P50, Mean, and P90 across canonical input sequence length buckets to reveal whether longer inputs yield shorter or longer generations.",
    "Decode Intensity (OSL / ISL Ratio) by Input Sequence Length": "Median and mean OSL/ISL ratio across canonical input sequence length buckets, showing how decode vs prefill dominance shifts with input sequence length.",
    "w-conc": "Exact event-sweep in-flight concurrency residency and time-series trajectories of concurrency and arrival vs completion rates.",
    "In-Flight Concurrency Residency": "Percentage of the active window duration spent at each exact concurrency level or concurrency range, computed from arrival-to-completion intervals.",
    "In-Flight Concurrency over Time": "Time-weighted average and exact peak in-flight request concurrency across elapsed run buckets.",
    "Arrival vs Completion Rate over Time": "Client request arrival rate versus completion rate (req/s) across elapsed run buckets, highlighting bursts and queue drain.",
    "w-multiturn": "Session turn-count distribution, input sequence length growth across turns, and reusable leading-prefix token share.",
    "Turns per Session Distribution": "Distribution of active window sessions by total conversation turn depth.",
    "ISL Growth by Turn Depth": "Input sequence length percentiles (P50, Mean, P90) grouped by conversation turn depth, showing context accumulation across turns.",

    // Hardware page
    "h-overview": "Target environment, accelerator profile, participating node and pod counts, allocated device memory, and aggregate host compute capacity for the simulation run.",
    "h-nodes": "Per-node hardware specifications across participating serving nodes, including roles, pod counts, accelerator models and capacities, device memory, CPU cores, host RAM, and OS/runtime versions. Click any node row to inspect its host and device telemetry.",
    "h-pods": "Per-pod accelerator, CPU, and host memory allocations across target serving and routing pods. Click a pod's node link to inspect host and device telemetry for that node.",
    "h-cache": "Configured KV cache memory hierarchy across device HBM, host CPU memory offload, and local storage offload, along with combined effective token capacity.",
    "Target Environment": "Target deployment cluster and namespace (or external endpoint URL) along with serving topology mode and deployment guide type.",
    "Accelerator Profile": "Hardware profile, vendor, and detected accelerator device models backing the target serving deployment (or recorded task hardware fallback when telemetry is unavailable).",
    "Serving Nodes & Pods": "Total participating serving nodes and target pods, broken down by serving role when disaggregated or aggregated.",
    "Allocated Accelerators & Device Memory": "Total allocated accelerator devices across participating serving nodes and aggregate device memory (HBM) capacity.",
    "Host Compute & Memory": "Aggregate physical CPU capacity (with allocatable cores) and host system RAM across participating serving nodes.",
    "Device HBM KV Capacity": "Configured KV cache token and byte capacity in accelerator high-bandwidth memory (HBM), with block geometry and P/D split when applicable.",
    "Host CPU KV Offload Capacity": "Configured KV cache offload capacity in host CPU RAM for multi-tier prefix cache spilling.",
    "Local Storage KV Capacity": "Configured KV cache offload capacity on local node NVMe or disk storage.",
    "Effective KV Cache Capacity": "Total usable KV cache token capacity across configured memory tiers and maximum theoretical concurrency at reference context length.",
  };

  const HELP_FORMULAS = {
    // Overview verdict, KPIs & sections
    "Automated Verdict": "8-rule deterministic check over Latency, Queueing, KV Pressure, Evictions & Routing",
    "Dominant Limit": "argmax_rule(Severity, Evidence_Confidence) across 8 bottleneck rules",
    "SLO Attainment": "SLO_Attainment (%) = (Good_Requests / Total_Window_Requests) × 100",
    "Goodput": "Goodput (req/s) = Good_Requests / Effective_Window_s  (× 3600 for req/hr)",
    "Completed / SLO-Compliant Requests": "Good_Requests = count(Successful ∧ TTFT ≤ SLO_TTFT ∧ TPOT ≤ SLO_TPOT ∧ E2E ≤ SLO_E2E)",
    "Request Throughput / Goodput": "Throughput = Total_Window_Requests / Effective_Window_s;  Goodput = Good_Requests / Effective_Window_s;  Output_Rate = Σ Output_Tokens / Effective_Window_s",
    "TTFT (P50 / P90 / P99)": "TTFT = t_first_token − t_request_start;  Pq = Quantile_q(sorted(TTFT))",
    "TPOT (P50 / P90 / P99)": "TPOT (ms/tok) = (t_last_token − t_first_token) / max(1, Output_Tokens − 1);  Pq = Quantile_q(sorted(TPOT))",
    "Error Rate": "Error_Rate (%) = (Failed_Requests / Total_Window_Requests) × 100",
    "Peak KV Cache Usage": "Peak_KV (%) = max_t((Used_Device_KV_Blocks / Total_Device_KV_Blocks) × 100);  Hit (%) = (ΔCached_Prompt_Tokens / ΔTotal_Prompt_Tokens) × 100",
    "Accelerator Power (Avg / Peak)": "Avg = mean_t(P_device);  Peak = max_t(P_device);  Energy_Wh = (1 / 3600) Σ 0.5 × (P_i + P_{i+1}) × Δt_i;  Eff = Total_Output_Tokens / Energy_Wh",
    "Warmup Phase (Auto 10%)": "Warmup_Window = [0, 0.10 × Run_Duration];  Stat_s(metric) for s ∈ {Min, P50, Mean, P90, P99, Max}",
    "Warmup Phase": "Warmup_Window = [0, Warmup_Seconds];  Stat_s(metric) for s ∈ {Min, P50, Mean, P90, P99, Max}",
    "Steady-State Profiling Window": "Steady_State_Window = [Warmup_End, Run_End];  Warmup_Ratio = Warmup_P50 / Steady_State_P50",
    "Warmup vs Steady-State Latency": "Stat_s(slice) for s ∈ {Min, P50, Mean, P90, P99, Max};  Warmup_Ratio = Warmup_P50 / Steady_State_P50",
    "o-verdict": "SLO_Attainment (%) = (Good_Requests / Total_Window_Requests) × 100;  Goodput = Good_Requests / Effective_Window_s",
    "o-kpis": "Effective_Window_s = max(Configured_Window_s, max(t_complete) − min(t_arrival));  Throughput = Total_Window_Requests / Effective_Window_s",
    "o-thr": "Bucket_Throughput = Bucket_Completed / Δt_bucket;  Bucket_Goodput = Bucket_Good / Δt_bucket;  Output_Rate = Σ Bucket_Output_Tokens / Δt_bucket",
    "o-perf": "Bucket_Stat(metric) = Stat(completed requests in bucket) for Min, P50, Mean, P90, P99, Max;  Shaded_Band = [P50, P90]",
    "o-conc": "Client_InFlight(t) = Σ 1[t_start ≤ t < t_complete]  vs  Engine_Running(t) = Σ_pods Active_Running(t)",
    "o-cold": "Stat_s(slice) for s ∈ {Min, P50, Mean, P90, P99, Max};  Warmup_Ratio = Warmup_P50 / Steady_State_P50",
    "o-power": "Pod_Avg_Power = mean_t(P_pod);  Pod_Peak_Power = max_t(P_pod);  Energy_Wh = (1 / 3600) Σ 0.5 × (P_i + P_{i+1}) × Δt_i",

    // Bottlenecks page
    "b-verdict": "Findings ranked by max(Severity, Evidence_Impact) across Latency, Queue, KV Cache, Routing & Skew checks",
    "b-timeline": "State(role, t) = Error (Failed > 0) | Evicting (Preempts > 0) | KV Pressure (KV ≥ 90%) | Queueing (Waiting > 0) | Healthy;  Time_in_State (%) = (Buckets_in_State / Total_Buckets) × 100",
    "b-stage-breakdown": "Total_Stage = EPP_Scheduling + Queue_Time + KV_Transfer + Prefill_Time + Server_TTFT_Residual + Client_Gateway_Network + Decode_Time",
    "Stage Share Summary": "E2E_Share (%) = (Stage_ms / Total_Stage_ms) × 100;  TTFT_Share (%) = (Stage_ms / TTFT_Stage_ms) × 100",
    "EPP Scheduling": "EPP_Scheduling = HistogramQuantile(q, ΔEPP_Scheduler_Buckets / Δt)",
    "Queue Time": "Queue_Time = P_Queue_Eff + max(0, D_Queue_Raw − KV_Transfer)",
    "KV Transfer (NIXL)": "KV_Transfer = min(NIXL_Xfer_Latency, D_Queue_Raw)",
    "Prefill Time": "Prefill_Time = HistogramQuantile(q, ΔEngine_Prefill_Buckets / Δt)",
    "Decode Time": "Decode_Time = HistogramQuantile(q, ΔEngine_Decode_Buckets / Δt)  (client fallback: max(0, Client_E2E − Client_TTFT))",
    "Decode First-Step Compute": "Decode_First_Step = Reconciled(D_Prefill)",
    "Prefill Engine / API Overhead": "Prefill_Engine_Overhead = Reconciled(max(0, P_TTFT − P_Queue_Eff − P_Prefill))",
    "Decode Handoff / SSE Overhead": "Decode_Handoff_Overhead = Reconciled(max(0, D_TTFT − D_Queue_Raw − D_Prefill))",
    "Engine / API Overhead": "Engine_API_Overhead = min(Total_Residual, max(0, Server_TTFT − (Queue + KV_Transfer + Prefill)))",
    "Server TTFT Residual": "Server_TTFT_Residual = min(Total_Residual, max(0, Server_TTFT − (Queue + KV_Transfer + Prefill)))",
    "Client / Gateway / Network": "Client_Gateway_Network = max(0, Client_TTFT − (EPP_Scheduling + Server_TTFT_Accounted))",
    "Server & Gateway Residual": "Residual = EPP_Scheduling + Server_TTFT_Residual + Client_Gateway_Network",
    "Client / Gateway / Routing": "Residual = EPP_Scheduling + Client_Gateway_Network",
    "P/D Engine TTFT (Queue + Prefill)": "PD_Engine_TTFT = max(0, Client_TTFT − (EPP_Scheduling + KV_Transfer))",
    "Gateway / EPP Overhead": "Gateway_Overhead = max(0, Client_TTFT − (EPP_Scheduling + Queue_Time + Prefill_Time))",
    "Client TTFT (Unattributed)": "Client_TTFT = Quantile_q(t_first_token − t_request_start)",
    "Unattributed TTFT": "Unattributed_TTFT = Client_TTFT  (exact client observation when stage attribution is unavailable)",
    "Decode Time (Est.)": "Decode_Est = max(0, Client_E2E − Client_TTFT)",
    "b-ttft-decomp": "Client_TTFT = Prefill + Decode_First_Step + Queue + KV_Transfer + Prefill_Engine_Overhead + Decode_Handoff_Overhead + EPP_Scheduling + Client_Gateway_Network  (or Unattributed_TTFT)",
    "b-cache-turns": "Avoided_Prefill (%) = (Cached_Prompt_Tokens / Total_Prompt_Tokens) × 100;  Followup_Regression_Rate (%) = (Count(Followup_TTFT > 1.5 × Turn1_TTFT_P50) / Followup_Turns) × 100",
    "b-routing": "Gap_pp = EPP_Predicted_Hit_% − Engine_Actual_Hit_%",
    "b-pods": "Skew_Ratio = max_pod(Pod_Load) / mean_pod(Pod_Load)  (engine pods, N ≥ 2, mean > 0)",
    "Load skew ratio (max / mean)": "Skew_Ratio = max_pod(Pod_Load) / mean_pod(Pod_Load)  (engine pods, N ≥ 2, mean > 0)",
    "Running": "Pod_Running_Mean = mean_t(Active_Running_Requests_pod(t))",
    "Waiting": "Pod_Waiting_Peak = max_t(Queued_Waiting_Requests_pod(t))  (or mean_t)",
    "Peak KV Cache (%)": "Peak_KV (%) = max_t((Used_Device_KV_Blocks / Total_Device_KV_Blocks) × 100)",
    "Prefix Cache Hit Rate (%)": "Prefix_Hit (%) = (ΔCached_Prompt_Tokens / ΔTotal_Prompt_Tokens) × 100",

    // Serving page
    "s-flow": "Per-Stage Frame(t) = mean/sum across role pods at timestamp t (or run-window mean in Summary mode)",
    "Endpoint Picker (EPP)": "EPP_Rate = ΔEPP_Requests / Δt;  InFlight = Active_Tracked_Requests(t)",
    "Workers": "Worker_Rate = Σ ΔCompleted_Requests / Δt;  KV (%) = (Used_KV_Blocks / Total_KV_Blocks) × 100",
    "Prefill": "Prefill_TokS = Σ_prefill (ΔPrompt_Tokens / Δt);  KV (%) = (Used_KV_Blocks / Total_KV_Blocks) × 100",
    "Decode": "Decode_TokS = Σ_decode (ΔGenerated_Tokens / Δt);  KV (%) = (Used_KV_Blocks / Total_KV_Blocks) × 100",
    "s-latency": "Server_Pq = HistogramQuantile(q, ΔEngine_Latency_Buckets / Δt)",
    "Server TTFT (P50)": "Server_TTFT_Pq = HistogramQuantile(q, ΔEngine_TTFT_Buckets / Δt)",
    "Server TPOT (P50)": "Server_TPOT_Pq = HistogramQuantile(q, ΔEngine_TPOT_Buckets / Δt)  (fallback: ΔEngine_ITL_Buckets)",
    "Server E2E (P50)": "Server_E2E_Pq = HistogramQuantile(q, ΔEngine_E2E_Buckets / Δt)",
    "Stage P50 (Queue / Prefill / Decode)": "Stage_P50 = HistogramQuantile(0.50, ΔEngine_Stage_Buckets / Δt)",
    "Stage P50 (KV Transfer / P/D TTFT / Decode Est.)": "KV_Transfer = NIXL_P50;  PD_Engine_TTFT = max(0, Client_TTFT − (EPP + KV_Transfer));  Decode_Est = max(0, Client_E2E − Client_TTFT)",
    "Server Latency Percentiles over Time": "Bucket_Pq(t) = HistogramQuantile(q, ΔEngine_Metric_Buckets / Δt);  Band = [P50, P90]",
    "Request Stage Latency Breakdown": "Total_Stage(t) = EPP_Scheduling(t) + Queue_Time(t) + [KV_Transfer(t)] + Prefill_Time(t) + Decode_Time(t)",
    "Prompt Token Throughput: Prefix Cache vs Prefill Compute": "Total_Prompt_TokS = (Local_Cache_TokS + External_KV_TokS) + Prefill_Compute_TokS",
    "s-engine": "Pool_Running(t) = Σ_pods Running(t);  Pool_Waiting(t) = Σ_pods Waiting(t);  TokS = Σ_pods (ΔTokens / Δt)",
    "Running Requests": "Running_Mean = mean_t(Σ_pods Active_Running(t));  Peak = max_t(Σ_pods Active_Running(t))",
    "Waiting Requests": "Waiting_Mean = mean_t(Σ_pods Queued_Waiting(t));  Peak = max_t(Σ_pods Queued_Waiting(t))",
    "Prefill Throughput": "Prefill_TokS = Σ_pods (ΔPrompt_Tokens / Δt)",
    "Decode Throughput": "Decode_TokS = Σ_pods (ΔGenerated_Tokens / Δt)",
    "Running vs Waiting Requests over Time": "Running(t) = Σ_role Active_Running(t);  Waiting(t) = Σ_role Queued_Waiting(t)",
    "Prefill vs Decode Token Throughput": "Prefill_TokS(t) = Σ_pods (ΔPrompt_Tokens / Δt);  Decode_TokS(t) = Σ_pods (ΔGenerated_Tokens / Δt)",
    "vLLM TPOT over Time": "Server_TPOT_Pq(t) = HistogramQuantile(q, ΔEngine_TPOT_Buckets / Δt)  (fallback: ΔEngine_ITL_Buckets)",
    "Prompt Token Breakdown": "Sent_Prompt = Local_Prefix_Cache + Recomputed_Prefix + Uncached_Prompt;  Ceiling (%) = (Theoretical_Reusable / Sent_Prompt) × 100",
    "Prompt Tokens per Request": "Mean_Prompt = Mean_Cached + Mean_Computed;  Cached (%) = (Mean_Cached / Mean_Prompt) × 100",
    "Prompt Token Sources by Role": "Role_Prompt_Tokens = Local_Prefix_Cache + External_KV_Transfer + Prefill_Compute",
    "Req/s": "Pod_RPS = mean_t(ΔCompleted_Requests_pod / Δt)",
    "Prefill (tok/s)": "Pod_Prefill_TokS = mean_t(ΔPrompt_Tokens_pod / Δt)",
    "Decode (tok/s)": "Pod_Decode_TokS = mean_t(ΔGenerated_Tokens_pod / Δt)",
    "KV Cache (%)": "Pod_KV (%) = max_t((Used_Device_KV_Blocks / Total_Device_KV_Blocks) × 100)",
    "s-load": "EPP_Rate = ΔEPP_Requests / Δt;  Sched_Pq = HistogramQuantile(q, ΔEPP_Sched_Buckets / Δt)",
    "EPP In-Flight Requests": "EPP_InFlight = mean_t(Active_EPP_Requests(t));  Peak = max_t(Active_EPP_Requests(t))",
    "EPP Scheduling Latency": "EPP_Sched_Pq = HistogramQuantile(q, ΔEPP_Scheduler_Buckets / Δt)",
    "EPP Plugin Latency": "EPP_Plugin_Pq = HistogramQuantile(q, ΔEPP_Plugin_Buckets / Δt)",
    "P/D Routing Ratio": "PD_Ratio (%) = (Disaggregated_PD_Requests / Total_Routed_Requests) × 100",
    "EPP Predicted Prefix Cache Hit Rate": "EPP_Predicted_Hit (%) = (ΔEPP_Matched_Prompt_Tokens / ΔEPP_Total_Prompt_Tokens) × 100",
    "EPP Load · In-Flight & Request Rate": "EPP_Rate(t) = ΔEPP_Requests / Δt  vs  Engine_Rate(t) = Σ_pods (ΔRequests / Δt)",
    "EPP Scheduling & Plugin Latency": "Sched_Pq(t) = HistogramQuantile(q, ΔEPP_Sched_Buckets / Δt);  Plugin_Pq(t) = HistogramQuantile(q, ΔEPP_Plugin_Buckets / Δt)",
    "Share %": "Endpoint_Share (%) = (Endpoint_Request_Rate / Σ_endpoints Endpoint_Request_Rate) × 100",
    "Rate (req/s)": "Endpoint_Rate = mean_t(ΔRouted_Requests_endpoint / Δt)",
    "In-Flight Token Load": "InFlight_Token_Load = mean_t(Active_Prompt_And_Decode_Tokens_endpoint(t))",
    "s-cache": "KV_Usage (%) = (Used_Device_KV_Blocks / Total_Device_KV_Blocks) × 100;  Tokens = Device_KV_Blocks × Block_Size",
    "KV Cache Usage (Peak / Mean)": "KV_Usage (%) = (Used_Device_KV_Blocks / Total_Device_KV_Blocks) × 100  (Peak = max_t, Mean = mean_t)",
    "HBM Capacity": "HBM_Capacity_Tokens = Σ_engines (Device_KV_Blocks × Block_Size_Tokens)",
    "Effective Capacity": "Effective_Capacity = Usable_Device_KV_Tokens;  Max_Concurrency = Effective_Capacity / Reference_Context_Tokens",
    "Block Size & Device KV Blocks": "Block_Size = Tokens_Per_KV_Block;  Device_KV_Blocks = Allocated_Accelerator_KV_Blocks",
    "HBM KV Cache Usage over Time": "KV_Usage(t) (%) = (Used_Device_KV_Blocks(t) / Total_Device_KV_Blocks(t)) × 100  (Threshold = 90%)",
    "KV Cache Tier Share (HBM · Host Memory · NVMe)": "Tier_Share_i(t) (%) = Active_KV_i(t) / (Active_KV_HBM(t) + Active_KV_Host(t) + Active_KV_NVMe(t)) × 100",
    "Block Size": "Block_Size = Tokens_Per_Paged_KV_Block",
    "Device KV Blocks": "Device_KV_Blocks = Total_Allocated_Accelerator_KV_Blocks",
    "KV Tokens": "KV_Tokens = Device_KV_Blocks × Block_Size",
    "Max Concurrency": "Max_Concurrency = KV_Tokens / Reference_Context_Tokens",
    "s-kvreuse": "Prefix_Hit (%) = (ΔCached_Prompt_Tokens / ΔTotal_Prompt_Tokens) × 100",
    "Prefix Cache Hit Rate": "Prefix_Hit (%) = (ΔCached_Prompt_Tokens / ΔTotal_Prompt_Tokens) × 100",
    "External Prefix Cache Hit Rate": "External_Hit (%) = (ΔExternal_Cached_Prompt_Tokens / ΔTotal_Prompt_Tokens) × 100",
    "Decode External KV Share (NIXL)": "Decode_External_KV_Share (%) = (ΔDecode_External_KV_Tokens / ΔDecode_Prompt_Tokens) × 100",
    "Local Prefix Cache Throughput": "Local_Cache_TokS = Σ_pods (ΔLocal_Cached_Prompt_Tokens / Δt)",
    "Prefill Compute Throughput": "Prefill_Compute_TokS = Σ_pods (ΔComputed_Prompt_Tokens / Δt);  Recomputed = max(0, Reusable − Cached)",
    "Prefix Cache Hit Rate over Time": "Local_Prefix_Cache + External_KV_Transfer + Recomputed_Prefix + Uncached_Prompt = 100%",
    "Prompt Token Sources over Time": "Total_Prompt_TokS(t) = Local_Prefix_Cache(t) + External_KV_Transfer(t) + Prefill_Compute(t)",
    "s-offload": "Tier_Usage (%) = (Used_Tier_KV / Total_Tier_KV) × 100;  Rate (MiB/s) = (ΔTransferred_Bytes / Δt) / 2^20",
    "VRAM KV Cache Usage": "VRAM_KV (%) = (Used_Device_KV_Blocks / Total_Device_KV_Blocks) × 100",
    "CPU KV Cache Usage": "CPU_KV (%) = (Used_Host_KV_Blocks / Total_Host_KV_Blocks) × 100",
    "NVMe KV Cache Usage": "NVMe_KV (%) = (Used_Disk_KV_Blocks / Total_Disk_KV_Blocks) × 100",
    "KV Offload Throughput": "Offload_Rate (MiB/s) = (ΔOffloaded_Bytes / Δt) / 2^20;  Total = ∫ Offload_Rate(t) dt",
    "KV Restore Throughput": "Restore_Rate (MiB/s) = (ΔRestored_Bytes / Δt) / 2^20;  Total = ∫ Restore_Rate(t) dt",
    "NIXL KV Transfer Throughput": "NIXL_Rate (MiB/s) = (ΔTransferred_Bytes / Δt) / 2^20;  P95 = HistogramQuantile(0.95, ΔNIXL_Latency_Buckets / Δt)",
    "KV Offload Lookup Sync Delay (P95)": "Lookup_Sync_P95 = HistogramQuantile(0.95, ΔKV_Lookup_Sync_Buckets / Δt)",
    "KV Cache Usage by Tier": "Tier_KV(t) (%) = (Used_Tier_KV(t) / Total_Tier_KV(t)) × 100  (tiers: VRAM, CPU, NVMe)",
    "KV Offload, Restore & NIXL Transfer Throughput": "Rate(t) (MiB/s) = (ΔTransferred_Bytes / Δt) / 2^20",
    "KV Offload & Restore Throughput": "Rate(t) (MiB/s) = (ΔTransferred_Bytes / Δt) / 2^20",
    "s-evict": "Eviction_Rate (blocks/min) = (ΔEvicted_KV_Blocks / Δt_s) × 60 ÷ Sampling_Fraction;  Preempt_Rate = ΔPreemptions / Δt",
    "KV Eviction · Block Churn, Residency & Preemptions": "Eviction_Rate (blocks/min) = (ΔEvicted_KV_Blocks / Δt_s) × 60 ÷ Sampling_Fraction;  Preempt_Rate = ΔPreemptions / Δt",
    "Total Preemptions": "Total_Preemptions = max(0, Preemptions(t_end) − Preemptions(t_start))",
    "Peak Preemption Rate": "Peak_Preempt_Rate (/s) = max_t(ΔPreempted_Requests / Δt)",
    "KV Cache Pressure": "Pressure_Detected iff Peak_KV ≥ 90% ∨ Total_Preemptions > 0",
    "KV Block Eviction Rate": "Eviction_Rate (blocks/min) = (ΔEvicted_KV_Blocks / Δt_s) × 60 ÷ Sampling_Fraction;  Total = ∫ (Rate / 60) dt",
    "KV Block Idle Time Before Eviction (P50 / P90)": "Idle_Before_Evict_Pq = HistogramQuantile(q, ΔKV_Block_Idle_Before_Evict_Buckets / Δt)",
    "KV Block Lifetime (P50 / P90)": "Lifetime_Pq = HistogramQuantile(q, ΔKV_Block_Lifetime_Buckets / Δt)",
    "KV Block Reuse Gap (P50 / P90)": "Reuse_Gap_Pq = HistogramQuantile(q, ΔKV_Block_Reuse_Gap_Buckets / Δt)",
    "Request Preemption Rate over Time": "Preempt_Rate(t) (/s) = ΔPreempted_Requests / Δt",
    "KV Block Eviction Rate (blocks/min)": "Eviction_Rate(t) (blocks/min) = (ΔEvicted_KV_Blocks / Δt_s) × 60 ÷ Sampling_Fraction",
    "KV Block Idle Time Before Eviction (s)": "Idle_Before_Evict_Pq(t) = HistogramQuantile(q, ΔKV_Block_Idle_Before_Evict_Buckets / Δt)",
    "KV Block Lifetime (s)": "Lifetime_Pq(t) = HistogramQuantile(q, ΔKV_Block_Lifetime_Buckets / Δt)",
    "KV Block Reuse Gap (s)": "Reuse_Gap_Pq(t) = HistogramQuantile(q, ΔKV_Block_Reuse_Gap_Buckets / Δt)",
    "s-power": "Energy_Wh = (1 / 3600) Σ 0.5 × (P_i + P_{i+1}) × Δt_i;  Eff = Total_Output_Tokens / Energy_Wh",
    "Accelerator Power": "Power_Mean = mean_t(P_device);  Peak = max_t(P_device);  Energy_Wh = (1 / 3600) Σ 0.5 × (P_i + P_{i+1}) × Δt_i",
    "Accelerator Utilization": "Util_Mean (%) = mean_t(Device_Compute_Util(t));  Peak = max_t(Device_Compute_Util(t))",
    "Accelerator Memory": "Mem_Used / Mem_Total = mean_t(Used_Device_Mem) / Total_Device_Mem;  Mem_BW (%) = mean_t(Device_Mem_BW_Util)",
    "Accelerator Temperature": "Temp_Mean (°C) = mean_t(Device_Temp(t));  Peak = max_t(Device_Temp(t));  Eff = Total_Output_Tokens / Energy_Wh",
    "Accelerator Power Draw over Time": "Power(t) (W) = Σ_or_mean_devices Device_Power_Watts(t)",
    "Accelerator Compute & Memory Bandwidth Utilization": "Compute_Util(t) (%) vs Mem_BW_Util(t) (%) sampled over run window",

    // Requests page
    "r-fidelity": "Drift (ms) = (t_actual_send − t_target_schedule) × 1000;  Actual_Send_RPS = Bucket_Arrived / Δt_bucket",
    "r-drift-hist": "Drift (ms) = (t_actual_send − t_target_schedule) × 1000;  Count(bin) = Σ 1[bin_lo ≤ Drift < bin_hi]",
    "r-lanes": "Greedy_Slot_Packing([t_arrival, t_complete]);  Tier: Cold < 10%, Partial 10–70%, Warm ≥ 70%",
    "Sessions over Time": "Greedy_Slot_Packing([t_arrival, t_complete]);  Tier: Cold < 10%, Partial 10–70%, Warm ≥ 70%",
    "r-buckets": "Bucket_Pq = Quantile_q(TTFT in bucket);  Point_i = (t_i, Seq_Len_i, Latency_i)",
    "TTFT by Input Sequence Length Bucket": "Bucket_Pq = Quantile_q(TTFT | Input_Tokens ∈ Bucket)",
    "TTFT by Prefix Cache Hit Bucket": "Overlap = Matched_Leading_Blocks / Prompt_Blocks;  Buckets: Cold (0%), Partial (0% < Overlap < 60%), Warm (≥ 60%)",
    "TTFT by Time and Input Sequence Length": "Point_i = (t_i, ISL_i, TTFT_i);  TTFT_i = t_first_token,i − t_request_start,i",
    "TPOT by Time and Output Sequence Length": "Point_i = (t_i, OSL_i, TPOT_i);  TPOT_i = (t_last_token,i − t_first_token,i) / max(1, Output_Tokens_i − 1)",
    "r-conv": "Session_Turns = count(requests in session);  Tree_Depth = max_branch_depth(session)",
    "Turns per Session": "Session_Count(k) = count(sessions with Turns == k)",
    "Conversation Tree Depth": "Session_Count(d) = count(sessions with Tree_Depth == d)",
    "r-cdf": "F(x) = (1 / N) Σ 1[x_i ≤ x] × 100%",
    "TTFT (ms)": "F(x) = (1 / N) Σ 1[TTFT_i ≤ x] × 100%  where TTFT = t_first_token − t_request_start",
    "TPOT (ms/tok)": "F(x) = (1 / N) Σ 1[TPOT_i ≤ x] × 100%  where TPOT = (t_last_token − t_first_token) / max(1, Output_Tokens − 1)",
    "E2E (ms)": "F(x) = (1 / N) Σ 1[E2E_i ≤ x] × 100%  where E2E = t_request_complete − t_request_start",
    "Schedule Drift (ms)": "F(x) = (1 / N) Σ 1[Drift_i ≤ x] × 100%  where Drift = (t_actual_send − t_target_schedule) × 1000",
    "TTFT": "TTFT = t_first_token − t_request_start",
    "TPOT": "TPOT (ms/tok) = (t_last_token − t_first_token) / max(1, Output_Tokens − 1)",
    "E2E": "E2E = t_request_complete − t_request_start ≈ TTFT + TPOT × max(0, Output_Tokens − 1)",
    "Schedule Drift": "Drift (ms) = (t_actual_send − t_target_schedule) × 1000",
    "Avg": "Avg = (1 / N) Σ x_i",
    "Min": "Min = min_i(x_i)",
    "P50": "P50 = Quantile_0.50(sorted(x))",
    "P90": "P90 = Quantile_0.90(sorted(x))",
    "P95": "P95 = Quantile_0.95(sorted(x))",
    "P99": "P99 = Quantile_0.99(sorted(x))",
    "Max": "Max = max_i(x_i)",
    "r-errors": "Error_Rate (%) = (Failed_Requests / Total_Window_Requests) × 100",
    "Latency": "Failure_Latency = t_error_or_timeout − t_request_start",

    // System page
    "y-kpis": "CPU = (1 − ΔIdle / ΔTotal) × 100;  Mem = ((Total − Avail) / Total) × 100;  IO_Rate = (ΔBytes / Δt) / 1024^k",
    "Host CPU %": "Host_CPU (%) = (1 − ΔIdle_CPU_Time / ΔTotal_CPU_Time) × 100",
    "Host Memory Avail": "Mem_Avail = Available_RAM  (out of Total_RAM)",
    "Disk Available": "Disk_Avail = Available_FS_Bytes / Total_FS_Bytes;  IO_Rate = (ΔDisk_Bytes / Δt) / 1024^k",
    "Host Power & Energy": "Host_Energy_Wh = (1 / 3600) Σ 0.5 × (P_i + P_{i+1}) × Δt_i",
    "Host CPU Utilization": "Host_CPU (%) = (1 − ΔIdle_CPU_Time / ΔTotal_CPU_Time) × 100",
    "Host Memory Usage": "Host_Mem_Used (%) = ((Total_RAM − Available_RAM) / Total_RAM) × 100;  Display = Bytes / 1024^k",
    "Disk I/O & Capacity": "Disk_IO = (ΔDisk_Bytes / Δt) / 1024^k;  Free_Ratio = Available_FS_Bytes / Total_FS_Bytes",
    "Network Throughput": "Net_RX_TX = (ΔNetwork_Bytes / Δt) / 1024^k",
    "Host Power": "Host_Power_Mean = mean_t(P_host);  Energy_Wh = (1 / 3600) Σ 0.5 × (P_i + P_{i+1}) × Δt_i",
    "Accelerator Power & Thermal": "Accel_Power_Mean = mean_t(P_device);  Peak_Temp = max_t(Device_Temp)",
    "y-nodes": "Per-node window Mean / Peak aggregated across host & device samples on each node",
    "Per-Node Summary": "Per-node window Mean / Peak aggregated across host & device samples on each node",
    "Roles & Pods": "Pod_Count = count(target serving pods scheduled on node)",
    "CPU (Mean / Peak)": "Node_CPU (%) = (1 − ΔIdle_CPU_Time / ΔTotal_CPU_Time) × 100  (Mean = mean_t, Peak = max_t)",
    "Memory (Used / Total · Peak %)": "Used = Total_RAM − Available_RAM;  Peak (%) = max_t((Used / Total_RAM) × 100)",
    "Disk I/O (R / W)": "Disk_R_W = mean_t(ΔDisk_Bytes / Δt) / 1024^k",
    "Free Disk": "Free_Disk = Available_FS_Bytes / Total_FS_Bytes",
    "Network (RX / TX)": "Net_RX_TX = mean_t(ΔNetwork_Bytes / Δt) / 1024^k",
    "Host Power (Mean / Peak)": "Host_Power_Mean = mean_t(P_host);  Peak = max_t(P_host)",
    "Accelerator (Count · Util · Power · Temp)": "Util = mean_t(Compute_Util);  Power = mean_t(P_device);  Peak_Temp = max_t(Device_Temp)",
    "y-cpu": "Host_CPU(t) (%) = (1 − ΔIdle_CPU_Time / ΔTotal_CPU_Time) × 100",
    "Charts Node Scope": "Scope ∈ {All Nodes (Aggregate)} ∪ Target_Nodes",
    "y-mem-disk": "Mem = Bytes / 1024^k;  Disk_IO = (ΔDisk_Bytes / Δt) / 1024^k",
    "Host Memory & Disk I/O": "Mem = Bytes / 1024^k;  Disk_IO = (ΔDisk_Bytes / Δt) / 1024^k",
    "y-net": "Net_RX_TX(t) = (ΔNetwork_Bytes / Δt) / 1024^k  (aggregate or per physical/fabric device)",
    "Network RX / TX Throughput": "Net_RX_TX(t) = (ΔNetwork_Bytes / Δt) / 1024^k  (aggregate or per physical/fabric device)",
    "Network Interface": "All_NICs(t) = Σ_d Rate(d, t);  By_NIC = top 4 active interfaces + Other (virtual interfaces excluded)",
    "y-power": "Host_Overhead_W(t) = Host_Power(t) − Σ_node_devices Accelerator_Power(t)",
    "Host & Accelerator Power": "Host_Overhead_W(t) = Host_Power(t) − Σ_node_devices Accelerator_Power(t)",

    // Workload page
    "w-config": "Replay_End = End_s ?? (Start_s + Duration_s × Scale_Factor);  Effective_Window = [0, Duration_s + Grace_s]",
    "Dataset": "Dataset = manifest.dataset.name || task.dataset || basename(trace_path)",
    "Replay Time Range": "Replay_Start = Start_s ?? 0;  Replay_End = End_s ?? (Replay_Start + Duration_s × Scale_Factor)",
    "Replay Duration & Warmup": "Profiling_Window = [Warmup_s, Duration_s + Grace_s];  Full_Window = [0, Duration_s + Grace_s]",
    "Pacing Mode": "Mode = Concurrency iff (trace_format == weka_public_dataset ∨ fixed_schedule == false ∨ Concurrency > 0), else Fixed Schedule",
    "Configured Concurrency": "Active_InFlight_Slots ≤ Configured_Concurrency  (or trace-driven arrival timestamps in Fixed Schedule)",
    "Scale Factor": "Target_Arrival_Interval = Source_Trace_Δt / Scale_Factor",
    "w-kpis": "Window_Requests = count(arrived ∈ Window);  Total_ISL = Σ Input_Tokens;  Total_OSL = Σ Output_Tokens",
    "Requests & Tokens": "Total = Completed + Failed;  Total_ISL = Σ Input_Tokens;  Total_OSL = Σ Output_Tokens",
    "Input Sequence Length (ISL P50)": "ISL_Pq = Quantile_q(sorted(Input_Tokens));  CV = pstdev(ISL) / mean(ISL)",
    "Output Sequence Length (OSL P50)": "OSL_Pq = Quantile_q(sorted(Output_Tokens));  CV = pstdev(OSL) / mean(OSL)",
    "OSL / ISL Ratio (P50)": "Ratio_i = Output_Tokens_i / Input_Tokens_i  (for Input_Tokens_i > 0);  Pq = Quantile_q(sorted(Ratio))",
    "In-Flight Concurrency (Mean)": "Mean = (Σ_k k × Δt_k) / W;  Pq = min{k : (Σ_{j≤k} Δt_j) / W ≥ q};  Peak = max_t Active(t)",
    "Mean Arrival Rate": "Mean_RPS = Arrivals / W;  Burstiness = Peak_Bucket_RPS / Mean_RPS;  CV = pstdev(Δt_arrival) / mean(Δt_arrival)",
    "Workload Regime Mix (OSL / ISL Ratio)": "Prefill-Heavy: Ratio < 0.25;  Balanced: 0.25 ≤ Ratio ≤ 1.0;  Decode-Heavy: Ratio > 1.0",
    "w-isl": "Bin_Share (%) = (Bin_Count / N_ISL) × 100;  CDF(x) = Quantile_q(sorted(Input_Tokens))",
    "Request ISL Distribution": "Bin_Count(b) = count(bin_start ≤ ISL < bin_end);  Share (%) = (Bin_Count / N_ISL) × 100",
    "Request ISL CDF": "F(x) = (1 / N_ISL) Σ 1[ISL_i ≤ x] × 100%",
    "w-osl": "Bin_Share (%) = (Bin_Count / N_OSL) × 100;  CDF(x) = Quantile_q(sorted(Output_Tokens))",
    "Request OSL Distribution": "Bin_Count(b) = count(bin_start ≤ OSL < bin_end);  Share (%) = (Bin_Count / N_OSL) × 100",
    "Request OSL CDF": "F(x) = (1 / N_OSL) Σ 1[OSL_i ≤ x] × 100%",
    "w-joint": "Regimes: Prefill-Heavy (< 0.25), Balanced (0.25–1.0), Decode-Heavy (> 1.0);  OSL_Stat(b) = Stat(OSL | ISL ∈ b)",
    "Input Sequence Length vs Output Sequence Length": "Point = (ISL_i, OSL_i);  Ratio_i = OSL_i / ISL_i (if ISL_i > 0);  Diagonal: OSL = ISL (1:1)",
    "OSL by Input Sequence Length Bucket": "OSL_Pq(b) = Quantile_q(OSL_i | ISL_i ∈ b);  OSL_Mean(b) = mean(OSL_i | ISL_i ∈ b)",
    "Decode Intensity (OSL / ISL Ratio) by Input Sequence Length": "Ratio_Pq(b) = Quantile_q(OSL_i / ISL_i | ISL_i ∈ b, ISL_i > 0)",
    "w-conc": "Active(t) = Σ 1[t_arrived ≤ t < t_completed];  Residency(b) (%) = (Σ_{k∈b} Δt_k / W) × 100",
    "In-Flight Concurrency Residency": "Share(b) (%) = (Duration_at_Concurrency_b / Window_Duration) × 100",
    "In-Flight Concurrency over Time": "InFlight_Avg(bucket) = (∫_{t0}^{t1} Active(t) dt) / (t1 − t0);  InFlight_Max(bucket) = max_{t∈[t0,t1)} Active(t)",
    "Arrival vs Completion Rate over Time": "Arrival_RPS(b) = Arrived(b) / Δt_b;  Completion_RPS(b) = Completed(b) / Δt_b",
    "w-multiturn": "Reusable_Prefix (%) = (Σ Reusable_Prefix_Tokens / Σ Input_Tokens) × 100",
    "Turns per Session Distribution": "Session_Share(k) (%) = (Sessions_with_k_Turns / Total_Sessions) × 100",
    "ISL Growth by Turn Depth": "ISL_Pq(turn) = Quantile_q(Input_Tokens | Turn ∈ bucket);  Prefix (%) = (Σ Reusable / Σ ISL) × 100",

    // Hardware page
    "h-overview": "Cluster_Totals = Σ_nodes (Allocated_Accelerators, Device_Memory, CPU_Cores, Host_RAM)",
    "h-nodes": "Node_Spec = {Roles, Pods, Accelerator_Model, Alloc / Cap_Devices, Device_Memory, CPU_Alloc / Cap, Host_RAM, Arch & OS}",
    "h-pods": "Pod_Alloc = {Pod, Role, Node, Allocated_Devices, Device_Memory, Σ_containers CPU_Req/Lim, Σ_containers Mem_Req/Lim}",
    "h-cache": "Effective_KV_Capacity = Usable(HBM_KV + Host_CPU_KV + Local_Storage_KV)",
    "Target Environment": "Target = (Endpoint_Mode == deployment) ? (Cluster / Namespace · Topology) : Sanitized_Endpoint_URL",
    "Accelerator Profile": "Profile = Server_Accelerator_Summary ?? Hardware_Profiles[0].models ?? Task_Hardware",
    "Serving Nodes & Pods": "Nodes = count(per_node);  Pods = count(target_serving_pods_by_role)",
    "Allocated Accelerators & Device Memory": "Total_Devices = Σ_nodes Allocated_Accelerators;  Total_Dev_Mem = Σ_nodes Device_Memory_Total_Bytes",
    "Host Compute & Memory": "CPU_Cap = Σ_nodes status.capacity.cpu;  Host_RAM = Σ_nodes (capacity.memory ?? Host_Memory_Total_Bytes)",
    "Device HBM KV Capacity": "HBM_Capacity_Tokens = Σ_engines (Device_KV_Blocks × Block_Size_Tokens)",
    "Host CPU KV Offload Capacity": "Host_CPU_KV_Capacity = Configured_Host_RAM_KV_Tokens / Bytes",
    "Local Storage KV Capacity": "Local_Storage_KV_Capacity = Configured_Local_Disk_KV_Tokens / Bytes",
    "Effective KV Cache Capacity": "Effective_Capacity = Usable_KV_Tokens;  Max_Concurrency = Effective_Capacity / Reference_Context_Tokens",
  };

  function normalizeHelpKey(raw) {
    return String(raw)
      .replace(/^Host & .+ Power$/i, "Host & Accelerator Power")
      .replace(/^.+ Power & Thermal$/i, "Accelerator Power & Thermal")
      .replace(/^.+ \(Count · Util · Power · Temp\)$/i, "Accelerator (Count · Util · Power · Temp)")
      .replace(/^(GPU|XPU|HPU|NPU|TPU|Accelerator)\s+/i, "Accelerator ");
  }

  function resolveHelpFromMap(map, key, explicitValue) {
    if (explicitValue) return explicitValue;
    if (!key) return null;
    const raw = String(key);
    if (map[raw]) return map[raw];
    const normalized = normalizeHelpKey(raw);
    if (map[normalized]) return map[normalized];
    const baseLabel = raw.replace(/\s*\(\d+\)\s*$/, "");
    if (map[baseLabel]) return map[baseLabel];
    const normalizedBase = normalizeHelpKey(baseLabel);
    if (map[normalizedBase]) return map[normalizedBase];
    return null;
  }

  function resolveHelpText(key, explicitText) {
    return resolveHelpFromMap(HELP_TEXTS, key, explicitText);
  }

  function resolveHelpFormula(key, explicitFormula) {
    return resolveHelpFromMap(HELP_FORMULAS, key, explicitFormula);
  }

  function helpBadge(title, text, formula) {
    const resolved = resolveHelpText(title, text);
    const resolvedFormula = resolveHelpFormula(title, formula);
    if (!resolved && !resolvedFormula) return null;
    const label = String(title || "metric");
    const icon = el("span", {
      className: "help-icon",
      tabindex: "0",
      role: "button",
      "aria-label": `Help: ${label}`,
      textContent: "?",
    });
    icon.addEventListener("mouseenter", (evt) => showHelpTip(evt, label, resolved, resolvedFormula));
    icon.addEventListener("mousemove", (evt) => showHelpTip(evt, label, resolved, resolvedFormula));
    icon.addEventListener("mouseleave", hideTip);
    icon.addEventListener("focus", () => showHelpTip(icon, label, resolved, resolvedFormula));
    icon.addEventListener("blur", hideTip);
    icon.addEventListener("click", (evt) => {
      evt.stopPropagation();
      showHelpTip(icon, label, resolved, resolvedFormula);
    });
    icon.addEventListener("keydown", (evt) => {
      if (evt.key === "Escape") {
        hideTip();
        icon.blur();
      } else if (evt.key === "Enter" || evt.key === " ") {
        evt.preventDefault();
        evt.stopPropagation();
        showHelpTip(icon, label, resolved, resolvedFormula);
      }
    });
    return icon;
  }

  const activeChartMounts = new Set();
  let pendingReflowHandle = null;
  const pendingReflowScopes = new Set();

  function nodeContains(parent, target) {
    if (!parent || !target) return false;
    if (parent === target) return true;
    if (typeof parent.contains === "function") return parent.contains(target);
    for (const ch of parent.children || []) {
      if (nodeContains(ch, target)) return true;
    }
    return false;
  }

  function measureContainerWidth(node) {
    if (!node || typeof node !== "object") return 0;
    if (typeof node.getBoundingClientRect === "function") {
      const rect = node.getBoundingClientRect();
      if (rect && Number.isFinite(rect.width) && rect.width > 0) {
        return rect.width;
      }
    }
    if (Number.isFinite(node.clientWidth) && node.clientWidth > 0) {
      return node.clientWidth;
    }
    return 0;
  }

  function removeContainerCrosshairSubscribers(container) {
    if (!container) return;
    const existing = container._chartCrosshairSub;
    if (existing) {
      state.crosshairSubscribers = state.crosshairSubscribers.filter((fn) => fn !== existing && (!fn || fn._chartContainer !== container));
      container._chartCrosshairSub = null;
    } else {
      state.crosshairSubscribers = state.crosshairSubscribers.filter((fn) => fn && fn._chartContainer !== container);
    }
  }

  function registerCrosshairSubscriber(container, fn) {
    if (typeof fn !== "function") return;
    removeContainerCrosshairSubscribers(container);
    fn._chartContainer = container || null;
    if (container) container._chartCrosshairSub = fn;
    state.crosshairSubscribers.push(fn);
  }

  function cleanupChartContainer(container) {
    if (!container) return;
    if (typeof container._chartResizeCleanup === "function") {
      const fn = container._chartResizeCleanup;
      container._chartResizeCleanup = null;
      fn();
    }
    if (container._hmResizeObserver && typeof container._hmResizeObserver.disconnect === "function") {
      container._hmResizeObserver.disconnect();
      container._hmResizeObserver = null;
    }
    if (container._swimlaneKeydownHandler && typeof container.removeEventListener === "function") {
      container.removeEventListener("keydown", container._swimlaneKeydownHandler);
      container._swimlaneKeydownHandler = null;
    }
    removeContainerCrosshairSubscribers(container);
  }

  function cleanupChartMountsIn(scopeNode) {
    if (!scopeNode) {
      for (const entry of Array.from(activeChartMounts)) {
        if (entry && typeof entry.cleanup === "function") entry.cleanup();
      }
      activeChartMounts.clear();
      return;
    }
    for (const entry of Array.from(activeChartMounts)) {
      if (nodeContains(scopeNode, entry.container)) {
        entry.cleanup();
      }
    }
  }

  function reflowVisibleCharts(scope) {
    for (const entry of Array.from(activeChartMounts)) {
      if (scope && scope !== document && !nodeContains(scope, entry.container)) {
        continue;
      }
      entry.reflow();
    }
  }

  function scheduleReflow(scope) {
    const targetScope = scope || document;
    pendingReflowScopes.add(targetScope);
    if (typeof requestAnimationFrame !== "function") {
      const scopes = Array.from(pendingReflowScopes);
      pendingReflowScopes.clear();
      for (const s of scopes) reflowVisibleCharts(s === document ? null : s);
      return;
    }
    if (pendingReflowHandle !== null) return;
    pendingReflowHandle = requestAnimationFrame(() => {
      pendingReflowHandle = null;
      const scopes = Array.from(pendingReflowScopes);
      pendingReflowScopes.clear();
      if (scopes.includes(document)) {
        reflowVisibleCharts(null);
      } else {
        for (const s of scopes) reflowVisibleCharts(s);
      }
    });
  }

  function mountResponsiveChart(container, options, renderAtWidth) {
    const opts = options || {};
    const minWidth = Math.max(240, Number(opts.minWidth || 280));
    const fallbackWidth = Math.max(minWidth, Number(opts.fallbackWidth || 560));

    cleanupChartContainer(container);

    if (container && container.isConnected === true) {
      container._wasConnected = true;
    }
    let lastMeasuredWidth = Math.round(measureContainerWidth(container));
    let lastRenderedWidth = Math.max(minWidth, lastMeasuredWidth > 0 ? lastMeasuredWidth : fallbackWidth);

    const invokeRender = (targetWidth) => {
      removeContainerCrosshairSubscribers(container);
      renderAtWidth(targetWidth);
    };

    invokeRender(lastRenderedWidth);

    let ro = null;
    const entry = {
      container,
      reflow() {
        if (container && container.isConnected === false && container._wasConnected) {
          entry.cleanup();
          return false;
        }
        if (container && container.isConnected === true) {
          container._wasConnected = true;
        }
        const measured = Math.round(measureContainerWidth(container));
        if (measured <= 0) return false;
        const nextWidth = Math.max(minWidth, measured);
        const shouldRender = (lastMeasuredWidth <= 0 && nextWidth !== lastRenderedWidth)
          || Math.abs(nextWidth - lastRenderedWidth) >= 2;
        lastMeasuredWidth = measured;
        if (shouldRender) {
          lastRenderedWidth = nextWidth;
          invokeRender(nextWidth);
          return true;
        }
        return false;
      },
      cleanup() {
        activeChartMounts.delete(entry);
        if (ro && typeof ro.disconnect === "function") {
          ro.disconnect();
        }
        removeContainerCrosshairSubscribers(container);
        if (container && container._chartResizeCleanup === entry.cleanup) {
          container._chartResizeCleanup = null;
        }
      },
    };

    if (typeof ResizeObserver === "function" && container) {
      ro = new ResizeObserver((entries) => {
        if (container.isConnected === false && container._wasConnected) {
          entry.cleanup();
          return;
        }
        if (container.isConnected === true) {
          container._wasConnected = true;
        }
        let entryWidth = 0;
        if (entries && entries.length) {
          for (const item of entries) {
            const cw = item && item.contentRect ? item.contentRect.width : 0;
            if (Number.isFinite(cw) && cw > 0) {
              entryWidth = cw;
              break;
            }
          }
        }
        const measured = Math.round(entryWidth > 0 ? entryWidth : measureContainerWidth(container));
        if (measured <= 0) return;
        const nextWidth = Math.max(minWidth, measured);
        const shouldRender = (lastMeasuredWidth <= 0 && nextWidth !== lastRenderedWidth)
          || Math.abs(nextWidth - lastRenderedWidth) >= 2;
        lastMeasuredWidth = measured;
        if (shouldRender) {
          lastRenderedWidth = nextWidth;
          invokeRender(nextWidth);
        }
      });
      ro.observe(container);
    }

    if (container) container._chartResizeCleanup = entry.cleanup;
    activeChartMounts.add(entry);
    return entry;
  }

  function broadcastCrosshair(ratio) {
    state.crosshairXRatio = ratio;
    const alive = [];
    for (const fn of state.crosshairSubscribers) {
      const host = fn && fn._chartContainer;
      if (host && host.isConnected === false && host._wasConnected) {
        continue;
      }
      if (host && host.isConnected === true) {
        host._wasConnected = true;
      }
      fn(ratio);
      alive.push(fn);
    }
    if (alive.length !== state.crosshairSubscribers.length) {
      state.crosshairSubscribers = alive;
    }
  }

  function resolveServerOriginEpoch(serverTelemetry, clientSeries) {
    if (clientSeries && Number.isFinite(Number(clientSeries.origin_epoch)) && Number(clientSeries.origin_epoch) > 1e9) {
      return Number(clientSeries.origin_epoch);
    }
    const winStart = serverTelemetry && serverTelemetry.window && serverTelemetry.window.start;
    if (typeof winStart === "string") {
      const ms = Date.parse(winStart);
      if (Number.isFinite(ms)) return ms / 1000;
    }
    const rawSeries = Array.isArray(serverTelemetry && serverTelemetry.series) ? serverTelemetry.series : [];
    for (const pt of rawSeries) {
      if (pt && typeof pt.timestamp === "string") {
        const ms = Date.parse(pt.timestamp);
        if (Number.isFinite(ms)) return ms / 1000;
      }
    }
    return 0;
  }

  function serverSeriesPoints(serverTelemetry, clientSeries) {
    const rawSeries = Array.isArray(serverTelemetry && serverTelemetry.series) ? serverTelemetry.series : [];
    const base = resolveServerOriginEpoch(serverTelemetry, clientSeries);
    return rawSeries
      .map((pt, idx) => {
        let t = idx * Number((serverTelemetry && serverTelemetry.step_seconds) || 5);
        if (typeof pt.timestamp === "string") {
          const ms = Date.parse(pt.timestamp);
          if (Number.isFinite(ms)) {
            const sec = ms / 1000;
            t = base > 1e9 ? Math.max(0, sec - base) : sec;
          }
        } else if (Number.isFinite(pt.t)) {
          t = pt.t;
        }
        return { ...pt, _t: t };
      })
      .sort((a, b) => a._t - b._t);
  }

  function hitPctFromPoint(p) {
    if (!p) return null;
    if (Number.isFinite(p.prefix_cache_hit_percent)) return p.prefix_cache_hit_percent;
    if (Number.isFinite(p.prefix_cache_hit_ratio)) return p.prefix_cache_hit_ratio * 100;
    return null;
  }

  function hitPctFromSummary(summary) {
    if (!summary) return null;
    if (summary.prefix_cache_hit_percent && Number.isFinite(summary.prefix_cache_hit_percent.mean)) {
      return summary.prefix_cache_hit_percent.mean;
    }
    if (summary.prefix_cache_hit_ratio && Number.isFinite(summary.prefix_cache_hit_ratio.mean)) {
      return summary.prefix_cache_hit_ratio.mean * 100;
    }
    return summary.prefix_cache_hit_pct ?? null;
  }

  function isFiniteMetric(v) {
    return v !== null && v !== undefined && v !== "" && Number.isFinite(Number(v));
  }

  function isFiniteChartPoint(p) {
    return Boolean(p && isFiniteMetric(p.x) && isFiniteMetric(p.y));
  }

  const BOUNDARY_LABEL_WIDTHS = {
    Warmup: 44,
    Drain: 32,
  };

  function hasPhaseBoundaries(warmupEnd, drainStart, minX, maxX, windowMode) {
    const hasWarmup = isFiniteMetric(warmupEnd) && Number(warmupEnd) > 0 && Number(warmupEnd) > minX && windowMode === "full";
    const hasDrain = isFiniteMetric(drainStart) && Number(drainStart) > minX && Number(drainStart) < maxX;
    return { hasWarmup, hasDrain };
  }

  function phaseBoundaryPad(basePadR, warmupEnd, drainStart, minX, maxX, windowMode) {
    const { hasWarmup, hasDrain } = hasPhaseBoundaries(warmupEnd, drainStart, minX, maxX, windowMode);
    return {
      l: 54,
      r: hasDrain ? Math.max(basePadR, BOUNDARY_LABEL_WIDTHS.Drain + 8) : basePadR,
      t: (hasWarmup || hasDrain) ? 22 : 16,
      b: 28,
    };
  }

  function placeBoundaryLabel(x, estWidth, minLeft, maxRight, side) {
    if (side === "left") {
      // Keep Warmup on the left of its boundary line (text-anchor="end"), clamped inside SVG bounds.
      const anchorX = Math.max(minLeft + estWidth, Math.min(maxRight, x - 4));
      return { x: anchorX, anchor: "end", left: anchorX - estWidth, right: anchorX };
    }
    // Keep Drain on the right of its boundary line (text-anchor="start"), clamped inside SVG bounds.
    const anchorX = Math.min(maxRight - estWidth, Math.max(minLeft, x + 4));
    return { x: anchorX, anchor: "start", left: anchorX, right: anchorX + estWidth };
  }

  function boundaryLabelsOverlap(a, b) {
    return !(a.right + 4 <= b.left || b.right + 4 <= a.left);
  }

  function appendBoundaryLabel(svg, labelText, pos, y, fill) {
    svg.appendChild(
      svgEl(
        "text",
        {
          x: Number(pos.x.toFixed(2)),
          y: Number(y.toFixed(2)),
          fill,
          "font-size": 10,
          "text-anchor": pos.anchor,
        },
        [document.createTextNode(labelText)]
      )
    );
  }

  function renderPhaseBoundaries(svg, opts) {
    const { pad, iw, ih, minX, maxX, sx, muted, warmupEnd, drainStart, windowMode } = opts;
    const plotLeft = pad.l;
    const plotRight = pad.l + iw;
    const svgWidth = opts.width || (plotRight + pad.r);
    const svgLeft = 4;
    const svgRight = svgWidth - 4;
    // Place labels in the reserved top band above plotTop so they stay clear of Y-axis ticks.
    const baseY = pad.t - 8;
    const staggerY = pad.t + 11;
    const { hasWarmup, hasDrain } = hasPhaseBoundaries(warmupEnd, drainStart, minX, maxX, windowMode);

    let wx = null;
    if (hasWarmup) {
      wx = Math.min(plotRight, Math.max(plotLeft, sx(Number(warmupEnd))));
      svg.appendChild(svgEl("rect", { x: plotLeft, y: pad.t, width: wx - plotLeft, height: ih, fill: "rgba(245, 166, 35, 0.10)" }));
      svg.appendChild(svgEl("line", { x1: wx, y1: pad.t, x2: wx, y2: pad.t + ih, stroke: "#f5a623", "stroke-dasharray": "4 3", "stroke-width": 1.2 }));
    }

    let dx = null;
    if (hasDrain) {
      dx = Math.min(plotRight, Math.max(plotLeft, sx(Number(drainStart))));
      svg.appendChild(svgEl("rect", { x: dx, y: pad.t, width: Math.max(0, plotRight - dx), height: ih, fill: "rgba(148, 163, 184, 0.10)" }));
      svg.appendChild(svgEl("line", { x1: dx, y1: pad.t, x2: dx, y2: pad.t + ih, stroke: muted, "stroke-dasharray": "4 3", "stroke-width": 1.2 }));
    }

    const warmupPos = wx !== null ? placeBoundaryLabel(wx, BOUNDARY_LABEL_WIDTHS.Warmup, svgLeft, svgRight, "left") : null;
    const drainPos = dx !== null ? placeBoundaryLabel(dx, BOUNDARY_LABEL_WIDTHS.Drain, svgLeft, svgRight, "right") : null;
    let drainY = baseY;

    if (warmupPos && drainPos && boundaryLabelsOverlap(warmupPos, drainPos)) {
      drainY = staggerY;
    }

    if (warmupPos) {
      appendBoundaryLabel(svg, "Warmup", warmupPos, baseY, muted);
    }
    if (drainPos) {
      appendBoundaryLabel(svg, "Drain", drainPos, drainY, muted);
    }
  }

  function resolveRunPhaseBoundaries(data) {
    const d = data || {};
    const cs = d.client_series || {};
    const fullWin = (cs.windows && cs.windows.full) || {};
    const manifest = d.run_manifest || d.manifest || {};
    const warmupVal = firstFinite(
      d.task && d.task.warmup_seconds,
      manifest.replay && manifest.replay.warmup_seconds,
      manifest.warmup_seconds,
      cs.warmup_seconds
    );
    const warmupEnd = warmupVal !== null ? Math.max(0, warmupVal) : 0;
    const dispatchDuration = isFiniteMetric(cs.dispatch_duration_seconds) ? Number(cs.dispatch_duration_seconds) : null;
    const fullDuration = isFiniteMetric(cs.duration_seconds)
      ? Number(cs.duration_seconds)
      : (isFiniteMetric(fullWin.duration_seconds) ? Number(fullWin.duration_seconds) : null);
    const explicitDrain = firstFinite(
      cs.drain_start_seconds,
      manifest.replay && manifest.replay.drain_start_seconds,
      manifest.drain_start_seconds
    );
    const drainStart = explicitDrain !== null
      ? explicitDrain
      : (dispatchDuration !== null && fullDuration !== null && (fullDuration - dispatchDuration) >= 3.0
          ? dispatchDuration
          : null);
    return { warmupEnd, drainStart };
  }

  function resolveConfiguredConcurrency(data) {
    if (!data || typeof data !== "object") return null;
    const rawBackend =
      (data.task && data.task.backend) ||
      (data.run_manifest && data.run_manifest.backend) ||
      (data.manifest && data.manifest.backend) ||
      "";
    const backend = String(rawBackend).trim().toLowerCase();
    if (backend === "trace-replayer" || backend === "trace_replayer") return null;

    const optionSources = [
      data.run_manifest && data.run_manifest.replay && data.run_manifest.replay.backend_options,
      data.run_manifest && data.run_manifest.backend_options,
      data.manifest && data.manifest.replay && data.manifest.replay.backend_options,
      data.manifest && data.manifest.backend_options,
      data.task && data.task.simulation && data.task.simulation.backend_options,
      data.task && data.task.backend_options,
      data.config && data.config.backend_options,
    ];
    const candidates = [];
    for (const opts of optionSources) {
      if (!opts || typeof opts !== "object") continue;
      candidates.push(opts.concurrency, opts.max_concurrency, opts.session_concurrency);
    }
    candidates.push(
      data.run_manifest && data.run_manifest.replay && data.run_manifest.replay.concurrency,
      data.manifest && data.manifest.replay && data.manifest.replay.concurrency,
      data.task && data.task.configured_concurrency,
      data.task && data.task.concurrency,
      data.config && data.config.concurrency
    );
    for (const v of candidates) {
      if (
        v !== null &&
        v !== undefined &&
        v !== "" &&
        typeof v !== "boolean" &&
        !Array.isArray(v) &&
        isFiniteMetric(v) &&
        Number(v) > 0
      ) {
        return Number(v);
      }
    }
    return null;
  }

  function positiveFinite(v) {
    if (
      v === null ||
      v === undefined ||
      v === "" ||
      typeof v === "boolean" ||
      Array.isArray(v) ||
      !isFiniteMetric(v)
    ) {
      return null;
    }
    const n = Number(v);
    return n > 0 ? n : null;
  }

  function resolveConfiguredSlos(data) {
    const d = data || {};
    const verdictSlo = (d.forensics && d.forensics.verdict && d.forensics.verdict.slo) || null;
    const manifestSlo = (d.run_manifest && d.run_manifest.slo) || (d.manifest && d.manifest.slo) || null;
    const sloSources = [verdictSlo, manifestSlo].filter((s) => s && typeof s === "object");
    const optionSources = [
      d.run_manifest && d.run_manifest.replay && d.run_manifest.replay.backend_options,
      d.run_manifest && d.run_manifest.backend_options,
      d.manifest && d.manifest.replay && d.manifest.replay.backend_options,
      d.manifest && d.manifest.backend_options,
      d.task && d.task.simulation && d.task.simulation.backend_options,
      d.task && d.task.backend_options,
    ].filter((s) => s && typeof s === "object");

    const pickMs = (sloKeys, optKey) => {
      for (const src of sloSources) {
        for (const [k, mult] of sloKeys) {
          const v = positiveFinite(src[k]);
          if (v !== null) return v * mult;
        }
      }
      for (const opts of optionSources) {
        const v = positiveFinite(opts[optKey]);
        if (v !== null) return v * 1000;
      }
      return null;
    };

    return {
      ttft_ms: pickMs([["ttft_s", 1000], ["ttft_ms", 1]], "ttft_slo"),
      tpot_ms: pickMs([["tpot_ms", 1], ["tpot_s", 1000]], "tpot_slo"),
      e2e_ms: pickMs([["e2e_s", 1000], ["e2e_ms", 1]], "e2e_slo"),
    };
  }

  function LineBandChart(container, config) {
    const height = config.height || 220;

    const rawSloCfg = config.sloLine && typeof config.sloLine === "object" ? config.sloLine : null;
    const sloVal = positiveFinite(rawSloCfg ? rawSloCfg.value : config.sloLine);

    const allX = [];
    const leftY = [];
    const rightY = [];
    for (const s of config.series || []) {
      for (const pt of s.points || []) {
        if (!isFiniteMetric(pt && pt.x)) continue;
        allX.push(Number(pt.x));
        const target = s.axis === "right" ? rightY : leftY;
        if (isFiniteMetric(pt.y)) target.push(Number(pt.y));
        if (isFiniteMetric(pt.hi)) target.push(Number(pt.hi));
        if (isFiniteMetric(pt.lo)) target.push(Number(pt.lo));
      }
    }
    if (!allX.length || (!leftY.length && !rightY.length)) {
      cleanupChartContainer(container);
      container.replaceChildren(el("div", { className: "empty-note", textContent: config.emptyText || "No time-series samples available." }));
      return null;
    }
    if (sloVal !== null) {
      leftY.push(sloVal);
    }

    const refLine =
      config.refLine &&
      typeof config.refLine === "object" &&
      config.refLine.value !== null &&
      config.refLine.value !== undefined &&
      config.refLine.value !== "" &&
      typeof config.refLine.value !== "boolean" &&
      !Array.isArray(config.refLine.value) &&
      isFiniteMetric(config.refLine.value) &&
      Number(config.refLine.value) > 0
        ? config.refLine
        : null;
    if (refLine) {
      leftY.push(Number(refLine.value));
    }

    const rawMinX = Math.min(...allX);
    const rawMaxX = Math.max(...allX);
    const singleX = rawMinX === rawMaxX;
    const minX = config.minX !== undefined ? config.minX : (singleX ? Math.max(0, rawMinX - 1) : rawMinX);
    const maxX = Math.max(minX + 0.001, config.maxX !== undefined ? config.maxX : (singleX ? rawMaxX + 1 : rawMaxX));
    const pad = phaseBoundaryPad(config.rightLabel ? 54 : 18, config.warmupEnd, config.drainStart, minX, maxX, state.windowMode);
    const ih = height - pad.t - pad.b;
    const minL = config.yMin !== undefined ? config.yMin : 0;
    const rawMaxL = leftY.length ? Math.max(...leftY) : 1;
    const defaultMaxL = rawMaxL > minL ? rawMaxL * 1.12 : minL + 1;
    const maxL = config.yMax !== undefined
      ? Math.max(minL + 1e-6, config.yMax)
      : (refLine ? Math.max(defaultMaxL, Number(refLine.value) + 1) : defaultMaxL);
    const minR = config.rightYMin !== undefined ? config.rightYMin : 0;
    const rawMaxR = rightY.length ? Math.max(...rightY) : 1;
    const maxR = config.rightYMax !== undefined ? Math.max(minR + 1e-6, config.rightYMax) : Math.max(1e-6, rawMaxR * 1.12);

    const syL = (y) => {
      const clampedY = config.yMax !== undefined ? Math.min(maxL, Math.max(minL, y)) : y;
      return pad.t + ih - ((clampedY - minL) / (maxL - minL)) * ih;
    };
    const syR = (y) => {
      const clampedY = config.rightYMax !== undefined ? Math.min(maxR, Math.max(minR, y)) : y;
      return pad.t + ih - ((clampedY - minR) / (maxR - minR)) * ih;
    };

    return mountResponsiveChart(container, { minWidth: 280, fallbackWidth: 560 }, (width) => {
      const iw = width - pad.l - pad.r;
      const sx = (x) => pad.l + ((x - minX) / (maxX - minX)) * iw;
      const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height, style: "display:block" });
    const gridColor = cssVar("--border");
    const muted = cssVar("--text-muted");
    const cardBg = cssVar("--bg-card") || "#171f2c";

    renderPhaseBoundaries(svg, {
      width,
      pad,
      iw,
      ih,
      minX,
      maxX,
      sx,
      muted,
      warmupEnd: config.warmupEnd,
      drainStart: config.drainStart,
      windowMode: state.windowMode,
    });

    for (let i = 0; i <= 4; i++) {
      const yVal = minL + ((maxL - minL) * i) / 4;
      const y = syL(yVal);
      svg.appendChild(svgEl("line", { x1: pad.l, y1: y, x2: pad.l + iw, y2: y, stroke: gridColor, "stroke-width": 0.8 }));
      svg.appendChild(svgEl("text", { x: pad.l - 6, y: y + 4, fill: muted, "font-size": 10, "text-anchor": "end" }, [document.createTextNode(axisNum(yVal, maxL))]));
    }
    for (let i = 0; i <= 4; i++) {
      const xVal = minX + ((maxX - minX) * i) / 4;
      const x = sx(xVal);
      svg.appendChild(svgEl("text", { x, y: height - 8, fill: muted, "font-size": 10, "text-anchor": "middle" }, [document.createTextNode(Math.round(xVal) + "s")]));
    }
    if (config.rightLabel && rightY.length) {
      for (let i = 0; i <= 4; i += 2) {
        const yVal = minR + ((maxR - minR) * i) / 4;
        const y = syR(yVal);
        svg.appendChild(svgEl("text", { x: pad.l + iw + 6, y: y + 4, fill: muted, "font-size": 10, "text-anchor": "start" }, [document.createTextNode(axisNum(yVal, maxR))]));
      }
    }

    const legend = el("div", { className: "chart-legend" });
    (config.series || []).forEach((s, idx) => {
      const color = s.color || PALETTE[idx % PALETTE.length];
      const sy = s.axis === "right" ? syR : syL;
      const rawPts = s.points || [];
      const pts = rawPts.filter(isFiniteChartPoint).map((p) => ({
        x: Number(p.x),
        y: Number(p.y),
        lo: isFiniteMetric(p.lo) ? Number(p.lo) : null,
        hi: isFiniteMetric(p.hi) ? Number(p.hi) : null,
      }));
      if (!pts.length) return;
      const bandPts = pts.filter((p) => p.lo !== null && p.hi !== null);
      if (bandPts.length > 1) {
        const upper = bandPts.map((p, i) => `${i === 0 ? "M" : "L"}${sx(p.x).toFixed(1)},${sy(p.hi).toFixed(1)}`).join(" ");
        const lower = bandPts.slice().reverse().map((p) => `L${sx(p.x).toFixed(1)},${sy(p.lo).toFixed(1)}`).join(" ");
        svg.appendChild(svgEl("path", { d: `${upper} ${lower} Z`, fill: color, opacity: 0.16 }));
      } else if (bandPts.length === 1 && bandPts[0].hi > bandPts[0].lo) {
        const bx = sx(bandPts[0].x).toFixed(1);
        svg.appendChild(svgEl("line", { x1: bx, y1: sy(bandPts[0].hi).toFixed(1), x2: bx, y2: sy(bandPts[0].lo).toFixed(1), stroke: color, "stroke-width": 4, "stroke-linecap": "round", opacity: 0.28 }));
      }
      const dash = s.strokeDasharray !== undefined ? s.strokeDasharray : (s.dashed ? "4 3" : null);
      const sw = s.strokeWidth !== undefined ? s.strokeWidth : (s.dashed ? 1.5 : 2);
      const op = s.opacity !== undefined && s.opacity !== null ? Number(s.opacity) : null;
      if (pts.length > 1) {
        const d = pts.map((p, i) => `${i === 0 ? "M" : "L"}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join(" ");
        svg.appendChild(svgEl("path", { d, fill: "none", stroke: color, "stroke-width": sw, "stroke-dasharray": dash, opacity: op }));
      }
      rawPts.forEach((pt, pIdx) => {
        if (!isFiniteChartPoint(pt)) return;
        const isolated = pts.length === 1 || (!isFiniteChartPoint(rawPts[pIdx - 1]) && !isFiniteChartPoint(rawPts[pIdx + 1]));
        if (!isolated) return;
        svg.appendChild(svgEl("circle", {
          cx: sx(Number(pt.x)).toFixed(1),
          cy: sy(Number(pt.y)).toFixed(1),
          r: pts.length === 1 ? 4 : 3.5,
          fill: color,
          stroke: cardBg,
          "stroke-width": 1.5,
          opacity: op,
        }));
      });
      const swatchStyle = dash
        ? `width:14px;height:0;border-top:2px ${dash.startsWith("2 ") || dash.startsWith("1.") ? "dotted" : "dashed"} ${color};background:transparent;border-radius:0${op !== null ? `;opacity:${op}` : ""}`
        : `background:${color}${op !== null ? `;opacity:${op}` : ""}`;
      legend.appendChild(el("span", { className: "legend-item" }, [el("span", { className: "legend-swatch", style: swatchStyle }), s.label]));
    });

    if (sloVal !== null) {
      const sloColor = (rawSloCfg && rawSloCfg.color) || "#ef5350";
      const sy = syL(sloVal);
      const sloText = config.sloLabel !== undefined
        ? config.sloLabel
        : (rawSloCfg && rawSloCfg.label !== undefined ? rawSloCfg.label : `SLO: ${msFmt(sloVal)}`);
      const sloLegend = config.sloLegendLabel !== undefined
        ? config.sloLegendLabel
        : (rawSloCfg && rawSloCfg.legendLabel !== undefined ? rawSloCfg.legendLabel : `SLO (${msFmt(sloVal)})`);
      svg.appendChild(
        svgEl("line", {
          x1: pad.l,
          y1: sy,
          x2: pad.l + iw,
          y2: sy,
          stroke: sloColor,
          "stroke-dasharray": "5 4",
          "stroke-width": 1.2,
          "data-slo-line": "true",
        })
      );
      if (sloText) {
        svg.appendChild(
          svgEl(
            "text",
            {
              x: pad.l + iw - 4,
              y: Math.max(pad.t + 11, sy - 5),
              fill: sloColor,
              "font-size": 10,
              "font-weight": 600,
              "text-anchor": "end",
            },
            [document.createTextNode(String(sloText))]
          )
        );
      }
      if (sloLegend) {
        legend.appendChild(
          el("span", { className: "legend-item" }, [
            el("span", {
              className: "legend-swatch",
              style: `width:14px;height:0;border-top:2px dashed ${sloColor};background:transparent;border-radius:0`,
            }),
            sloLegend,
          ])
        );
      }
    }

    if (refLine) {
      const refColor = refLine.color || PALETTE[2];
      const sy = syL(Number(refLine.value));
      svg.appendChild(
        svgEl("line", {
          x1: pad.l,
          y1: sy,
          x2: pad.l + iw,
          y2: sy,
          stroke: refColor,
          "stroke-dasharray": "6 4",
          "stroke-width": 1.5,
          "data-ref-line": "configured-concurrency",
        })
      );
      if (refLine.label) {
        svg.appendChild(
          svgEl(
            "text",
            {
              x: pad.l + iw - 4,
              y: Math.max(pad.t + 11, sy - 5),
              fill: refColor,
              "font-size": 10,
              "font-weight": 600,
              "text-anchor": "end",
            },
            [document.createTextNode(String(refLine.label))]
          )
        );
      }
      legend.appendChild(
        el("span", { className: "legend-item" }, [
          el("span", {
            className: "legend-swatch",
            style: `width:14px;height:0;border-top:2px dashed ${refColor};background:transparent;border-radius:0`,
          }),
          refLine.legendLabel || refLine.label,
        ])
      );
    }

    const crosshair = svgEl("line", { x1: pad.l, y1: pad.t, x2: pad.l, y2: pad.t + ih, stroke: cssVar("--accent"), "stroke-width": 1, "stroke-dasharray": "3 3", visibility: "hidden" });
    svg.appendChild(crosshair);

    const updateCrosshair = (ratio) => {
      if (ratio === null || ratio < 0 || ratio > 1) {
        crosshair.setAttribute("visibility", "hidden");
        return;
      }
      const cx = pad.l + ratio * iw;
      crosshair.setAttribute("x1", cx.toFixed(1));
      crosshair.setAttribute("x2", cx.toFixed(1));
      crosshair.setAttribute("visibility", "visible");
    };
    registerCrosshairSubscriber(container, updateCrosshair);
    if (state.crosshairXRatio !== null) updateCrosshair(state.crosshairXRatio);

    const overlay = svgEl("rect", { x: pad.l, y: pad.t, width: iw, height: ih, fill: "transparent", style: "cursor:crosshair" });
    overlay.addEventListener("mousemove", (evt) => {
      const rect = svg.getBoundingClientRect();
      if (!rect || !rect.width) return;
      const relX = ((evt.clientX - rect.left) / rect.width) * width;
      const ratio = Math.min(1, Math.max(0, (relX - pad.l) / iw));
      broadcastCrosshair(ratio);
      const t = minX + ratio * (maxX - minX);
      const lines = [`t = ${fmtDec(t)}s`];
      for (const s of config.series || []) {
        const pts = (s.points || []).filter(isFiniteChartPoint);
        let best = null;
        let bestDist = Infinity;
        for (const p of pts) {
          const dist = Math.abs(Number(p.x) - t);
          if (dist < bestDist) {
            bestDist = dist;
            best = p;
          }
        }
        if (best && isFiniteMetric(best.y)) {
          const yVal = Number(best.y);
          const unitSuffix = config.yUnit ? (config.yUnit === "%" ? "%" : ` ${config.yUnit}`) : "";
          const formattedY = typeof s.formatValue === "function" ? s.formatValue(yVal) : `${fmtDec(yVal)}${unitSuffix}`;
          const hiVal = !s.hideBandTooltip && isFiniteMetric(best.hi) ? Number(best.hi) : null;
          const formattedHi = hiVal !== null
            ? (typeof s.formatValue === "function" ? s.formatValue(hiVal) : `${fmtDec(hiVal)}${unitSuffix}`)
            : null;
          const band = formattedHi !== null ? ` (P90 ${formattedHi})` : "";
          lines.push(`${s.label}: ${formattedY}${band}`);
        }
      }
      showTip(evt, lines.join("\n"));
    });
    overlay.addEventListener("mouseleave", () => {
      broadcastCrosshair(null);
      hideTip();
    });
    svg.appendChild(overlay);
    container.replaceChildren(svg, legend);
    });
  }

  function StackedAreaChart(host, opts) {
    const config = opts || {};
    const seriesList = Array.isArray(config.series) ? config.series : [];
    const xSet = new Set();
    const seriesMaps = seriesList.map((s) => {
      const m = new Map();
      for (const pt of s.points || []) {
        if (!pt || !isFiniteMetric(pt.x)) continue;
        const x = Number(pt.x);
        xSet.add(x);
        if (isFiniteMetric(pt.y)) {
          m.set(x, Math.max(0, Number(pt.y)));
        }
      }
      return m;
    });
    const sortedX = Array.from(xSet).sort((a, b) => a - b);
    const timeline = sortedX.map((x) => {
      let anyFinite = false;
      for (const m of seriesMaps) {
        if (m.has(x)) {
          anyFinite = true;
          break;
        }
      }
      if (!anyFinite) {
        return { x, isGap: true, layers: null, total: null };
      }
      let acc = 0;
      const layers = seriesMaps.map((m) => {
        const val = m.has(x) ? m.get(x) : 0;
        const y0 = acc;
        acc += val;
        return { val, y0, y1: acc };
      });
      return { x, isGap: false, layers, total: acc };
    });
    const validRows = timeline.filter((r) => !r.isGap);
    if (!seriesList.length || !validRows.length) {
      cleanupChartContainer(host);
      host.replaceChildren(el("div", { className: "empty-note", textContent: config.emptyText || "No time-series samples available." }));
      return null;
    }

    const height = config.height || 220;

    const rawMinX = sortedX[0];
    const rawMaxX = sortedX[sortedX.length - 1];
    const singleX = rawMinX === rawMaxX;
    const minX = config.minX !== undefined ? config.minX : (singleX ? Math.max(0, rawMinX - 1) : rawMinX);
    const maxX = Math.max(minX + 0.001, config.maxX !== undefined ? config.maxX : (singleX ? rawMaxX + 1 : rawMaxX));
    const pad = phaseBoundaryPad(18, config.warmupEnd, config.drainStart, minX, maxX, state.windowMode);
    const ih = height - pad.t - pad.b;
    const minL = config.yMin !== undefined ? config.yMin : 0;
    let rawMaxL = Math.max(0, ...validRows.map((r) => r.total));
    if (isFiniteMetric(config.sloLine)) {
      rawMaxL = Math.max(rawMaxL, Number(config.sloLine));
    }
    const maxL = config.yMax !== undefined ? Math.max(minL + 1e-6, config.yMax) : (rawMaxL > minL ? rawMaxL * 1.12 : minL + 1);

    const sy = (y) => {
      const clampedY = config.yMax !== undefined ? Math.min(maxL, Math.max(minL, y)) : Math.max(minL, y);
      return pad.t + ih - ((clampedY - minL) / (maxL - minL)) * ih;
    };

    return mountResponsiveChart(host, { minWidth: 280, fallbackWidth: 560 }, (width) => {
      const iw = width - pad.l - pad.r;
      const sx = (x) => pad.l + ((x - minX) / (maxX - minX)) * iw;
      const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height, style: "display:block" });
    const gridColor = cssVar("--border");
    const muted = cssVar("--text-muted");
    const cardBg = cssVar("--bg-card") || "#171f2c";

    renderPhaseBoundaries(svg, {
      width,
      pad,
      iw,
      ih,
      minX,
      maxX,
      sx,
      muted,
      warmupEnd: config.warmupEnd,
      drainStart: config.drainStart,
      windowMode: state.windowMode,
    });

    const yTicks = Array.isArray(config.yTicks) && config.yTicks.length
      ? config.yTicks
      : [0, 1, 2, 3, 4].map((i) => minL + ((maxL - minL) * i) / 4);
    for (const yVal of yTicks) {
      const y = sy(yVal);
      const yLabel = typeof config.yAxisFormat === "function" ? config.yAxisFormat(yVal, maxL) : axisNum(yVal, maxL);
      svg.appendChild(svgEl("line", { x1: pad.l, y1: y, x2: pad.l + iw, y2: y, stroke: gridColor, "stroke-width": 0.8 }));
      svg.appendChild(svgEl("text", { x: pad.l - 6, y: y + 4, fill: muted, "font-size": 10, "text-anchor": "end" }, [document.createTextNode(yLabel)]));
    }
    for (let i = 0; i <= 4; i++) {
      const xVal = minX + ((maxX - minX) * i) / 4;
      const x = sx(xVal);
      const xTick = typeof config.xFormat === "function" ? config.xFormat(xVal) : `${Math.round(xVal)}s`;
      svg.appendChild(svgEl("text", { x, y: height - 8, fill: muted, "font-size": 10, "text-anchor": "middle" }, [document.createTextNode(xTick)]));
    }

    if (isFiniteMetric(config.sloLine)) {
      const sySlo = sy(Number(config.sloLine));
      svg.appendChild(svgEl("line", { x1: pad.l, y1: sySlo, x2: pad.l + iw, y2: sySlo, stroke: "#ef5350", "stroke-dasharray": "5 4", "stroke-width": 1.2 }));
    }

    const segments = [];
    let currentSeg = [];
    for (const row of timeline) {
      if (row.isGap) {
        if (currentSeg.length) {
          segments.push(currentSeg);
          currentSeg = [];
        }
      } else {
        currentSeg.push(row);
      }
    }
    if (currentSeg.length) segments.push(currentSeg);

    const fillOpacity = isFiniteMetric(config.fillOpacity) ? Number(config.fillOpacity) : 0.32;
    for (const seg of segments) {
      if (seg.length === 1) {
        const row = seg[0];
        const cx = sx(row.x);
        const barW = Math.min(18, Math.max(8, iw * 0.035));
        const barX = Math.min(pad.l + iw - barW, Math.max(pad.l, cx - barW / 2));
        const dotX = barX + barW / 2;
        seriesList.forEach((s, sIdx) => {
          const color = s.color || PALETTE[sIdx % PALETTE.length];
          const layer = row.layers[sIdx];
          const yTop = sy(layer.y1);
          const yBot = sy(layer.y0);
          const barH = Math.max(0, yBot - yTop);
          if (barH > 0) {
            svg.appendChild(svgEl("rect", {
              x: barX.toFixed(1),
              y: yTop.toFixed(1),
              width: barW.toFixed(1),
              height: barH.toFixed(1),
              fill: color,
              "fill-opacity": fillOpacity,
              stroke: color,
              "stroke-width": 1.25,
            }));
          }
          if (layer.val > 0 || (sIdx === 0 && row.total === 0)) {
            svg.appendChild(svgEl("circle", {
              cx: dotX.toFixed(1),
              cy: yTop.toFixed(1),
              r: 3.5,
              fill: color,
              stroke: cardBg,
              "stroke-width": 1.5,
            }));
          }
        });
      } else {
        const segAllZero = seg.every((r) => r.total === 0);
        seriesList.forEach((s, sIdx) => {
          const layerHasData = seg.some((r) => r.layers[sIdx].val > 0);
          if (!layerHasData && !(sIdx === 0 && segAllZero)) return;
          const color = s.color || PALETTE[sIdx % PALETTE.length];
          const topD = seg.map((r, i) => `${i === 0 ? "M" : "L"}${sx(r.x).toFixed(1)},${sy(r.layers[sIdx].y1).toFixed(1)}`).join(" ");
          const botD = seg.slice().reverse().map((r) => `L${sx(r.x).toFixed(1)},${sy(r.layers[sIdx].y0).toFixed(1)}`).join(" ");
          svg.appendChild(svgEl("path", {
            d: `${topD} ${botD} Z`,
            fill: color,
            "fill-opacity": fillOpacity,
            stroke: "none",
          }));
          svg.appendChild(svgEl("path", {
            d: topD,
            fill: "none",
            stroke: color,
            "stroke-width": 1.75,
            "stroke-linejoin": "round",
            "stroke-linecap": "round",
            "stroke-dasharray": s.dashed ? "4 3" : null,
          }));
        });
      }
    }

    const legend = el("div", { className: "chart-legend" });
    seriesList.forEach((s, idx) => {
      const color = s.color || PALETTE[idx % PALETTE.length];
      legend.appendChild(el("span", { className: "legend-item" }, [el("span", { className: "legend-swatch", style: `background:${color}` }), s.label]));
    });

    const crosshair = svgEl("line", { x1: pad.l, y1: pad.t, x2: pad.l, y2: pad.t + ih, stroke: cssVar("--accent"), "stroke-width": 1, "stroke-dasharray": "3 3", visibility: "hidden" });
    svg.appendChild(crosshair);

    const updateCrosshair = (ratio) => {
      if (ratio === null || ratio < 0 || ratio > 1) {
        crosshair.setAttribute("visibility", "hidden");
        return;
      }
      const cx = pad.l + ratio * iw;
      crosshair.setAttribute("x1", cx.toFixed(1));
      crosshair.setAttribute("x2", cx.toFixed(1));
      crosshair.setAttribute("visibility", "visible");
    };
    registerCrosshairSubscriber(host, updateCrosshair);
    if (state.crosshairXRatio !== null) updateCrosshair(state.crosshairXRatio);

    const formatStackedValue = (v) => {
      if (typeof config.yFormat === "function") return config.yFormat(v);
      if (config.yUnit === "ms") return msFmt(v);
      if (config.yUnit === "%") return pct(v);
      return config.yUnit ? `${fmtDec(v)} ${config.yUnit}` : fmtDec(v);
    };

    const overlay = svgEl("rect", { x: pad.l, y: pad.t, width: iw, height: ih, fill: "transparent", style: "cursor:crosshair" });
    overlay.addEventListener("mousemove", (evt) => {
      const rect = svg.getBoundingClientRect();
      if (!rect || !rect.width) return;
      const relX = ((evt.clientX - rect.left) / rect.width) * width;
      const ratio = Math.min(1, Math.max(0, (relX - pad.l) / iw));
      broadcastCrosshair(ratio);
      const t = minX + ratio * (maxX - minX);
      let best = null;
      let bestDist = Infinity;
      for (const row of validRows) {
        const dist = Math.abs(row.x - t);
        if (dist < bestDist) {
          bestDist = dist;
          best = row;
        }
      }
      if (!best) return hideTip();
      const headerText = typeof config.xFormat === "function" ? config.xFormat(best.x) : `t = ${fmtDec(best.x)}s`;
      const items = seriesList.map((s, sIdx) => ({
        label: s.label,
        color: s.color || PALETTE[sIdx % PALETTE.length],
        valueText: formatStackedValue(best.layers[sIdx].val),
      }));
      showStackedTip(evt, headerText, items, formatStackedValue(best.total));
    });
    overlay.addEventListener("mouseleave", () => {
      broadcastCrosshair(null);
      hideTip();
    });
    svg.appendChild(overlay);
    host.replaceChildren(svg, legend);
    });
  }

  function StackedBarAreaChart(container, config) {
    const items = config.items || [];
    if (!items.length) {
      cleanupChartContainer(container);
      container.replaceChildren(el("div", { className: "empty-note", textContent: config.emptyText || "No breakdown data available." }));
      return null;
    }
    const height = config.height || 210;
    const hasXLabel = Boolean(config.xLabel);
    const hasYLabel = Boolean(config.yLabel);
    const pad = { l: hasYLabel ? 64 : 54, r: 16, t: 16, b: hasXLabel ? 46 : 36 };
    const ih = height - pad.t - pad.b;
    const grouped = Boolean(config.grouped);
    const formatValue = typeof config.formatValue === "function" ? config.formatValue : (val) => num(val, 2);
    const yTickFormat = typeof config.yAxisFormat === "function" ? config.yAxisFormat : axisNum;

    const totals = items.map((item) => {
      const finiteVals = (item.segments || [])
        .map((seg) => (isFiniteMetric(seg && seg.value) ? Math.max(0, Number(seg.value)) : null))
        .filter((v) => v !== null);
      if (grouped) {
        return finiteVals.length ? Math.max(0, ...finiteVals) : 0;
      }
      return finiteVals.reduce((acc, v) => acc + v, 0);
    });
    const maxVal = Math.max(1e-6, Math.max(...totals) * 1.15);
    const sy = (v) => pad.t + ih - (v / maxVal) * ih;

    return mountResponsiveChart(container, { minWidth: 280, fallbackWidth: 540 }, (width) => {
      const iw = width - pad.l - pad.r;
      const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height, style: "display:block" });
    const gridColor = cssVar("--border");
    const muted = cssVar("--text-muted");

    for (let i = 0; i <= 4; i++) {
      const yVal = (maxVal * i) / 4;
      const y = sy(yVal);
      svg.appendChild(svgEl("line", { x1: pad.l, y1: y, x2: pad.l + iw, y2: y, stroke: gridColor, "stroke-width": 0.8 }));
      svg.appendChild(svgEl("text", { x: pad.l - 6, y: y + 4, fill: muted, "font-size": 10, "text-anchor": "end" }, [document.createTextNode(yTickFormat(yVal, maxVal))]));
    }

    const slotW = iw / items.length;
    const seenLegend = new Map();
    items.forEach((item, idx) => {
      const segs = item.segments || [];
      const cx = pad.l + idx * slotW + slotW / 2;
      const rawLabel = String(item.label ?? "");
      const maxChars = items.length === 1 ? 36 : Math.max(14, Math.floor(slotW / 7));
      const dispLabel = rawLabel.length > maxChars ? `${rawLabel.slice(0, Math.max(1, maxChars - 1))}…` : rawLabel;
      svg.appendChild(svgEl("text", { x: cx, y: hasXLabel ? pad.t + ih + 15 : height - 10, fill: muted, "font-size": 10, "text-anchor": "middle" }, [document.createTextNode(dispLabel)]));
      if (grouped) {
        const subW = Math.max(6, Math.min(28, (slotW * 0.72) / Math.max(1, segs.length)));
        const startX = cx - (subW * segs.length) / 2;
        segs.forEach((seg, sIdx) => {
          const color = seg.color || PALETTE[sIdx % PALETTE.length];
          const opacity = isFiniteMetric(seg.opacity) ? Number(seg.opacity) : 1;
          if (!seenLegend.has(seg.label)) seenLegend.set(seg.label, { color, opacity });
          if (!isFiniteMetric(seg.value)) return;
          const val = Math.max(0, Number(seg.value));
          const y = sy(val);
          const h = pad.t + ih - y;
          const attrs = { x: startX + sIdx * subW + 1, y, width: Math.max(3, subW - 2), height: Math.max(0, h), fill: color, rx: 2 };
          if (opacity < 1) attrs["fill-opacity"] = opacity;
          const r = svgEl("rect", attrs);
          r.addEventListener("mousemove", (evt) => showTip(evt, `${item.label} · ${seg.label}: ${formatValue(val)}${seg.extra ? " (" + seg.extra + ")" : ""}`));
          r.addEventListener("mouseleave", hideTip);
          svg.appendChild(r);
        });
      } else {
        const barW = Math.max(10, Math.min(54, slotW * 0.58));
        let acc = 0;
        segs.forEach((seg, sIdx) => {
          const color = seg.color || PALETTE[sIdx % PALETTE.length];
          const opacity = isFiniteMetric(seg.opacity) ? Number(seg.opacity) : 1;
          if (!seenLegend.has(seg.label)) seenLegend.set(seg.label, { color, opacity });
          if (!isFiniteMetric(seg.value)) return;
          const val = Math.max(0, Number(seg.value));
          const yTop = sy(acc + val);
          const yBot = sy(acc);
          acc += val;
          const attrs = { x: cx - barW / 2, y: yTop, width: barW, height: Math.max(0, yBot - yTop), fill: color };
          if (opacity < 1) attrs["fill-opacity"] = opacity;
          const r = svgEl("rect", attrs);
          r.addEventListener("mousemove", (evt) => {
            if (Array.isArray(item.tooltipItems) && item.tooltipItems.length) {
              showStackedTip(evt, item.tooltipTitle || item.label, item.tooltipItems, item.tooltipTotal || formatValue(totals[idx]), item.tooltipNote);
            } else {
              showTip(evt, `${item.label} · ${seg.label}: ${formatValue(val)}`);
            }
          });
          r.addEventListener("mouseleave", hideTip);
          svg.appendChild(r);
        });
      }
    });

    if (hasXLabel) {
      svg.appendChild(svgEl("text", {
        x: (pad.l + iw / 2).toFixed(2),
        y: height - 6,
        fill: muted,
        "font-size": 10.5,
        "text-anchor": "middle",
      }, [document.createTextNode(String(config.xLabel))]));
    }
    if (hasYLabel) {
      svg.appendChild(svgEl("text", {
        transform: `translate(14 ${(pad.t + ih / 2).toFixed(2)}) rotate(-90)`,
        fill: muted,
        "font-size": 10.5,
        "text-anchor": "middle",
      }, [document.createTextNode(String(config.yLabel))]));
    }

    const legend = el("div", { className: "chart-legend" });
    for (const [label, meta] of seenLegend.entries()) {
      const swatchStyle = `background:${meta.color}${meta.opacity < 1 ? `;opacity:${meta.opacity}` : ""}`;
      legend.appendChild(el("span", { className: "legend-item" }, [el("span", { className: "legend-swatch", style: swatchStyle }), label]));
    }
    container.replaceChildren(svg, legend);
    });
  }

  function HorizontalStackedBar(opts) {
    const segs = Array.isArray(opts && opts.segments) ? opts.segments : [];
    const segSum = segs.reduce((acc, s) => acc + Math.max(0, Number(s.value || 0)), 0);
    const total = isFiniteMetric(opts && opts.totalValue) && Number(opts.totalValue) > 0
      ? Number(opts.totalValue)
      : segSum;
    const fmtVal = typeof (opts && opts.formatValue) === "function" ? opts.formatValue : intFmt;
    const unitSuffix = opts && opts.unitLabel !== undefined ? String(opts.unitLabel) : " tok";
    const tipItems = segs.map((s) => {
      const val = Math.max(0, Number(s.value || 0));
      const p = total > 0 ? (val / total) * 100 : 0;
      return {
        label: s.label,
        color: s.color,
        valueText: `${fmtVal(val)}${unitSuffix} (${pct(p)})`,
      };
    });
    const hasCeiling = isFiniteMetric(opts && opts.ceilingPct);
    const wrap = el("div", { className: `hbar-wrap${hasCeiling ? "" : " no-ceiling"}` });
    const track = el("div", { className: `hbar-track${opts && opts.compact ? " compact" : ""}` });

    segs.forEach((s) => {
      const val = Math.max(0, Number(s.value || 0));
      if (val <= 0 || total <= 0) return;
      const pctW = Math.min(100, Math.max(0, (val / total) * 100));
      const segNode = el("div", {
        className: "hbar-seg",
        style: `width:${pctW.toFixed(3)}%;background:${s.color}`,
      });
      track.appendChild(segNode);
    });

    track.addEventListener("mousemove", (evt) => {
      showStackedTip(evt, (opts && opts.headerText) || "Breakdown", tipItems, (opts && opts.totalText) || `${fmtVal(total)}${unitSuffix}`);
    });
    track.addEventListener("mouseleave", hideTip);
    wrap.appendChild(track);

    if (hasCeiling) {
      const cPct = clampPct(opts.ceilingPct);
      const edgeCls = cPct < 12 ? " edge-left" : (cPct > 88 ? " edge-right" : "");
      wrap.appendChild(
        el("div", { className: "hbar-ceiling", style: `left:${cPct.toFixed(2)}%` }, [
          el("span", { className: `hbar-ceiling-label${edgeCls}`, textContent: `Ceiling ${pct(cPct)}` }),
        ])
      );
    }
    return wrap;
  }

  function renderHbarLegend(segments) {
    return el(
      "div",
      { className: "chart-legend" },
      (segments || []).map((s) =>
        el("span", { className: "legend-item" }, [
          el("span", { className: "legend-swatch", style: `background:${s.color}` }),
          s.label,
        ])
      )
    );
  }

  function renderStatStrip(items) {
    const children = [];
    (items || []).forEach((item) => {
      if (!item) return;
      if (children.length > 0) {
        children.push(el("span", { textContent: "·", "aria-hidden": "true" }));
      }
      children.push(
        el("span", { className: "stat-strip-item" }, [
          item.label ? `${item.label}: ` : "",
          el("strong", { textContent: item.value }),
        ])
      );
    });
    return el("div", { className: "stat-strip" }, children);
  }

  function timedSeriesPoints(rawSeries, serverTelemetry, clientSeries) {
    const base = resolveServerOriginEpoch(serverTelemetry, clientSeries);
    const stepSec = Number((serverTelemetry && serverTelemetry.step_seconds) || 5);
    return (Array.isArray(rawSeries) ? rawSeries : [])
      .map((pt, idx) => {
        let t = idx * stepSec;
        if (pt && typeof pt.timestamp === "string") {
          const ms = Date.parse(pt.timestamp);
          if (Number.isFinite(ms)) {
            const sec = ms / 1000;
            t = base > 1e9 ? Math.max(0, sec - base) : sec;
          }
        } else if (pt && Number.isFinite(pt.t)) {
          t = pt.t;
        }
        return { ...pt, _t: t };
      })
      .sort((a, b) => a._t - b._t);
  }

  function integrateRatePoints(pts, key, stepSeconds, fallbackMean, fallbackDuration) {
    const valid = [];
    for (const p of pts || []) {
      const t = p && p._t;
      const v = p && p[key];
      if (isFiniteMetric(t) && isFiniteMetric(v) && Number(v) >= 0) {
        valid.push({ t: Number(t), v: Number(v) });
      }
    }
    valid.sort((a, b) => a.t - b.t);
    const effStep = Math.max(1, Number(stepSeconds) || 5);
    if (valid.length === 1) {
      return Math.round(valid[0].v * effStep);
    }
    if (valid.length > 1) {
      const maxGap = Math.max(1, 3 * effStep);
      let total = 0;
      let integrated = false;
      for (let i = 0; i < valid.length - 1; i++) {
        const dt = valid[i + 1].t - valid[i].t;
        if (dt > 0 && dt <= maxGap) {
          total += 0.5 * (valid[i].v + valid[i + 1].v) * dt;
          integrated = true;
        }
      }
      return Math.round(integrated ? total : valid.reduce((acc, item) => acc + item.v * effStep, 0));
    }
    if (isFiniteMetric(fallbackMean) && Number(fallbackMean) >= 0 && isFiniteMetric(fallbackDuration) && Number(fallbackDuration) > 0) {
      return Math.round(Number(fallbackMean) * Number(fallbackDuration));
    }
    return null;
  }

  function deriveTokenFlowData(st, cs, ss, srvPts, summary, derived, durationSec, stepSec, isDisaggArg, roleSourcesArg) {
    const isDisagg = isDisaggArg !== undefined ? Boolean(isDisaggArg) : isDisaggregatedReport(st, null);
    const rSources = Array.isArray(roleSourcesArg)
      ? roleSourcesArg
      : deriveRoleTokenSourcesData(st, cs, srvPts, (st && st.role_series) || [], summary, derived, (st && st.per_pod) || [], durationSec, stepSec, isDisagg);
    const prefillRow = (rSources || []).find((r) => !r._pool_fallback && String((r && r.role) || "").trim().toLowerCase() === "prefill" && Number((r && r.total_tokens) || 0) > 0) || null;
    const prefillAvoidedRatio = prefillRow
      ? Math.min(1, Math.max(0, (Number(prefillRow.local_cache_tokens || 0) + Number(prefillRow.external_kv_tokens || 0)) / Number(prefillRow.total_tokens)))
      : null;
    const prefillAvoidedPct = prefillAvoidedRatio !== null ? prefillAvoidedRatio * 100 : null;

    const rawTf = derived && derived.token_flow;
    const hasRawTf = Boolean(rawTf && isFiniteMetric(rawTf.sent_tokens) && Number(rawTf.sent_tokens) > 0);
    const rawCachedPct = hasRawTf
      ? (isFiniteMetric(rawTf.cached_pct) ? Number(rawTf.cached_pct) : (Math.max(0, Number(rawTf.cached_tokens || 0)) / Number(rawTf.sent_tokens)) * 100)
      : null;
    const isLegacyPdTokenFlow = Boolean(
      isDisagg
      && prefillRow
      && hasRawTf
      && rawTf.role_scope !== "prefill"
      && ((rawCachedPct >= 99.5 && prefillAvoidedPct < 95) || Math.abs(rawCachedPct - prefillAvoidedPct) > 5)
    );

    if (hasRawTf && !isLegacyPdTokenFlow) {
      const sent = Number(rawTf.sent_tokens);
      const cached = Math.max(0, Number(rawTf.cached_tokens || 0));
      const recomputed = Math.max(0, Number(rawTf.recomputed_tokens || 0));
      const coldNew = Math.max(0, Number(rawTf.cold_new_tokens ?? Math.max(0, sent - cached - recomputed)));
      return {
        sent_tokens: sent,
        cached_tokens: cached,
        recomputed_tokens: recomputed,
        cold_new_tokens: coldNew,
        cached_pct: isFiniteMetric(rawTf.cached_pct) ? Number(rawTf.cached_pct) : (cached / sent) * 100,
        recomputed_pct: isFiniteMetric(rawTf.recomputed_pct) ? Number(rawTf.recomputed_pct) : (recomputed / sent) * 100,
        cold_new_pct: isFiniteMetric(rawTf.cold_new_pct) ? Number(rawTf.cold_new_pct) : (coldNew / sent) * 100,
        theoretical_cached_tokens: isFiniteMetric(rawTf.theoretical_cached_tokens) ? Number(rawTf.theoretical_cached_tokens) : null,
        theoretical_ceiling_pct: isFiniteMetric(rawTf.theoretical_ceiling_pct) ? clampPct(rawTf.theoretical_ceiling_pct) : null,
      };
    }

    const csTf = (cs && cs.token_flow) || {};
    const wins = (cs && cs.windows) || {};
    const fullWin = wins.full || wins.profiling || {};
    const winReqCount = firstFinite(fullWin.request_count, fullWin.total_requests);
    const clientInputTok = firstFinite(
      wins.full && wins.full.total_input_tokens,
      wins.profiling && wins.profiling.total_input_tokens,
      isFiniteMetric(fullWin.input_tokens && fullWin.input_tokens.avg) && winReqCount !== null && winReqCount > 0
        ? Math.round(Number(fullWin.input_tokens.avg) * Number(winReqCount))
        : null
    );
    const csSentTok = isFiniteMetric(csTf.sent_tokens) && Number(csTf.sent_tokens) > 0 ? Math.round(Number(csTf.sent_tokens)) : null;

    if (isDisagg && prefillRow && (isLegacyPdTokenFlow || !hasRawTf)) {
      const sentRaw = firstFinite(
        hasRawTf ? rawTf.sent_tokens : null,
        csSentTok,
        clientInputTok !== null && clientInputTok > 0 ? clientInputTok : null,
        prefillRow.total_tokens
      );
      if (!sentRaw || sentRaw <= 0) return null;
      const sent = Math.round(sentRaw);
      const cached = Math.min(sent, Math.max(0, Math.round(sent * prefillAvoidedRatio)));
      const cachedPct = (cached / sent) * 100;
      const remaining = Math.max(0, sent - cached);

      const rawReusableTok = firstFinite(
        positiveFinite(csTf.raw_session_reusable_tokens),
        positiveFinite(ss && ss.reusable_prefix_tokens)
      );
      const rawOverlapPct = positiveFinite(ss && ss.theoretical_prefix_overlap_pct);
      const avoidedRatio = positiveFinite(derived && derived.avoided_prefill_ratio) !== null
        ? clampPct(derived.avoided_prefill_ratio)
        : null;

      let theoreticalCached = null;
      let ceilingPct = null;
      if (rawReusableTok !== null) {
        theoreticalCached = Math.min(sent, Math.max(cached, Math.round(rawReusableTok)));
        ceilingPct = clampPct((theoreticalCached / sent) * 100);
      } else if (rawOverlapPct !== null) {
        theoreticalCached = Math.min(sent, Math.max(cached, Math.round(sent * (Math.min(100, rawOverlapPct) / 100))));
        ceilingPct = clampPct((theoreticalCached / sent) * 100);
      } else if (avoidedRatio !== null && avoidedRatio > cachedPct) {
        theoreticalCached = Math.min(sent, Math.max(cached, Math.round(sent * (Math.min(100, avoidedRatio) / 100))));
        ceilingPct = clampPct((theoreticalCached / sent) * 100);
      }

      const rolePts = timedSeriesPoints((st && st.role_series) || [], st, cs);
      const sbrPrefill = (st && st.summary_by_role && st.summary_by_role.prefill) || {};
      const drvPrefill = (derived && derived.role_summary && derived.role_summary.prefill) || {};
      const recompRoleTok = integrateRatePoints(
        rolePts,
        "prefill_engine_prompt_recomputed_tps",
        stepSec,
        podStatMean(sbrPrefill, "engine_prompt_recomputed_tps") ?? firstFinite(drvPrefill.engine_prompt_recomputed_tps),
        durationSec
      );
      const recompPoolTok = integrateRatePoints(srvPts, "engine_prompt_recomputed_tps", stepSec, podStatMean(summary, "engine_prompt_recomputed_tps"), durationSec);
      const vllmRecompTok = recompRoleTok !== null ? recompRoleTok : recompPoolTok;

      const recompFromCeiling = theoreticalCached !== null ? Math.max(0, theoreticalCached - cached) : 0;
      const recompFromRate = vllmRecompTok !== null ? Math.max(0, vllmRecompTok) : 0;
      const recomputed = Math.min(remaining, Math.max(recompFromCeiling, recompFromRate));
      const coldNew = Math.max(0, sent - cached - recomputed);
      return {
        sent_tokens: sent,
        cached_tokens: cached,
        recomputed_tokens: recomputed,
        cold_new_tokens: coldNew,
        cached_pct: cachedPct,
        recomputed_pct: (recomputed / sent) * 100,
        cold_new_pct: (coldNew / sent) * 100,
        theoretical_cached_tokens: theoreticalCached,
        theoretical_ceiling_pct: ceilingPct,
      };
    }

    const localCacheTok = integrateRatePoints(srvPts, "engine_prompt_local_cache_tps", stepSec, podStatMean(summary, "engine_prompt_local_cache_tps"), durationSec);
    const extKvTok = integrateRatePoints(srvPts, "engine_prompt_external_tps", stepSec, podStatMean(summary, "engine_prompt_external_tps"), durationSec);
    const freshComputeTok = integrateRatePoints(srvPts, "engine_prompt_local_compute_tps", stepSec, podStatMean(summary, "engine_prompt_local_compute_tps"), durationSec);
    const recompRateTok = integrateRatePoints(srvPts, "engine_prompt_recomputed_tps", stepSec, podStatMean(summary, "engine_prompt_recomputed_tps"), durationSec);
    const promInputTok = integrateRatePoints(srvPts, "input_token_rate_tps", stepSec, podStatMean(summary, "input_token_rate_tps"), durationSec);
    const engineSumTok = (localCacheTok !== null || extKvTok !== null || freshComputeTok !== null)
      ? (localCacheTok || 0) + (extKvTok || 0) + (freshComputeTok || 0)
      : null;
    const sent = csSentTok !== null
      ? csSentTok
      : ((clientInputTok !== null && clientInputTok > 0)
          ? Math.round(clientInputTok)
          : (promInputTok !== null && promInputTok > 0 ? promInputTok : (engineSumTok !== null && engineSumTok > 0 ? engineSumTok : null)));
    if (!sent || sent <= 0) return null;

    const hitPct = hitPctFromSummary(summary) ?? (isFiniteMetric(derived && derived.avoided_prefill_ratio) ? clampPct(derived.avoided_prefill_ratio) : null);
    const csCachedTok = isFiniteMetric(csTf.client_cached_tokens)
      ? Number(csTf.client_cached_tokens)
      : (isFiniteMetric(csTf.cached_tokens) ? Number(csTf.cached_tokens) : null);
    let cached = (localCacheTok !== null || extKvTok !== null)
      ? (localCacheTok || 0) + (extKvTok || 0)
      : (hitPct !== null ? Math.round(sent * (clampPct(hitPct) / 100)) : csCachedTok);
    if (cached === null) return null;
    cached = Math.min(sent, Math.max(0, Math.round(cached)));
    const cachedPct = (cached / sent) * 100;
    const remaining = Math.max(0, sent - cached);

    const rawReusableTok = isFiniteMetric(csTf.raw_session_reusable_tokens) && Number(csTf.raw_session_reusable_tokens) > 0
      ? Number(csTf.raw_session_reusable_tokens)
      : (isFiniteMetric(ss && ss.reusable_prefix_tokens) && Number(ss.reusable_prefix_tokens) > 0
          ? Number(ss.reusable_prefix_tokens)
          : null);
    const rawOverlapPct = isFiniteMetric(ss && ss.theoretical_prefix_overlap_pct) && Number(ss.theoretical_prefix_overlap_pct) > 0
      ? clampPct(ss.theoretical_prefix_overlap_pct)
      : null;
    const avoidedRatio = isFiniteMetric(derived && derived.avoided_prefill_ratio)
      ? clampPct(derived.avoided_prefill_ratio)
      : null;

    let theoreticalCached = null;
    let ceilingPct = null;
    if (rawReusableTok !== null) {
      theoreticalCached = Math.min(sent, Math.max(cached, Math.round(rawReusableTok)));
      ceilingPct = clampPct((theoreticalCached / sent) * 100);
    } else if (rawOverlapPct !== null) {
      ceilingPct = Math.min(100, Math.max(rawOverlapPct, cachedPct));
      theoreticalCached = Math.min(sent, Math.max(cached, Math.round(sent * (ceilingPct / 100))));
    } else if (avoidedRatio !== null && avoidedRatio > cachedPct) {
      ceilingPct = Math.min(100, avoidedRatio);
      theoreticalCached = Math.min(sent, Math.max(cached, Math.round(sent * (ceilingPct / 100))));
    }

    const recompFromCeiling = theoreticalCached !== null ? Math.max(0, theoreticalCached - cached) : 0;
    const recompFromRate = recompRateTok !== null ? Math.max(0, recompRateTok) : 0;
    const recomputed = Math.min(remaining, Math.max(recompFromCeiling, recompFromRate));
    const coldNew = Math.max(0, sent - cached - recomputed);
    return {
      sent_tokens: sent,
      cached_tokens: cached,
      recomputed_tokens: recomputed,
      cold_new_tokens: coldNew,
      cached_pct: cachedPct,
      recomputed_pct: (recomputed / sent) * 100,
      cold_new_pct: (coldNew / sent) * 100,
      theoretical_cached_tokens: theoreticalCached,
      theoretical_ceiling_pct: ceilingPct,
    };
  }

  function derivePromptTokensPerReqData(cs, summary, derived, tokenFlow, isDisagg, prefillAvoidedPct) {
    const rawPpr = derived && derived.prompt_tokens_per_request;
    if (rawPpr && isFiniteMetric(rawPpr.mean_prompt_tokens) && Number(rawPpr.mean_prompt_tokens) > 0) {
      const meanPrompt = Number(rawPpr.mean_prompt_tokens);
      if (isDisagg && rawPpr.role_scope !== "prefill") {
        const healedCachedPct = firstFinite(
          isFiniteMetric(prefillAvoidedPct) ? clampPct(prefillAvoidedPct) : null,
          tokenFlow && isFiniteMetric(tokenFlow.cached_pct) ? clampPct(tokenFlow.cached_pct) : null
        );
        if (healedCachedPct !== null) {
          const meanCached = meanPrompt * (healedCachedPct / 100);
          const meanComputed = Math.max(0, meanPrompt - meanCached);
          return {
            mean_prompt_tokens: meanPrompt,
            mean_cached_tokens: meanCached,
            mean_computed_tokens: meanComputed,
            cached_pct: healedCachedPct,
            prompt_p50: isFiniteMetric(rawPpr.prompt_p50) ? Number(rawPpr.prompt_p50) : null,
            prompt_p90: isFiniteMetric(rawPpr.prompt_p90) ? Number(rawPpr.prompt_p90) : null,
            computed_p50: null,
            computed_p90: null,
            source: rawPpr.source || "vllm_histogram",
          };
        }
        const rawMeanCached = isFiniteMetric(rawPpr.mean_cached_tokens) ? Math.max(0, Number(rawPpr.mean_cached_tokens)) : 0;
        const rawMeanComputed = isFiniteMetric(rawPpr.mean_computed_tokens) ? Math.max(0, Number(rawPpr.mean_computed_tokens)) : Math.max(0, meanPrompt - rawMeanCached);
        return {
          mean_prompt_tokens: meanPrompt,
          mean_cached_tokens: rawMeanCached,
          mean_computed_tokens: rawMeanComputed,
          cached_pct: isFiniteMetric(rawPpr.cached_pct) ? clampPct(rawPpr.cached_pct) : clampPct((rawMeanCached / meanPrompt) * 100),
          prompt_p50: isFiniteMetric(rawPpr.prompt_p50) ? Number(rawPpr.prompt_p50) : null,
          prompt_p90: isFiniteMetric(rawPpr.prompt_p90) ? Number(rawPpr.prompt_p90) : null,
          computed_p50: null,
          computed_p90: null,
          source: rawPpr.source || "vllm_histogram",
        };
      }
      const meanCached = isFiniteMetric(rawPpr.mean_cached_tokens) ? Math.max(0, Number(rawPpr.mean_cached_tokens)) : 0;
      const meanComputed = isFiniteMetric(rawPpr.mean_computed_tokens) ? Math.max(0, Number(rawPpr.mean_computed_tokens)) : Math.max(0, meanPrompt - meanCached);
      return {
        mean_prompt_tokens: meanPrompt,
        mean_cached_tokens: meanCached,
        mean_computed_tokens: meanComputed,
        cached_pct: isFiniteMetric(rawPpr.cached_pct) ? clampPct(rawPpr.cached_pct) : clampPct((meanCached / meanPrompt) * 100),
        prompt_p50: isFiniteMetric(rawPpr.prompt_p50) ? Number(rawPpr.prompt_p50) : null,
        prompt_p90: isFiniteMetric(rawPpr.prompt_p90) ? Number(rawPpr.prompt_p90) : null,
        computed_p50: isFiniteMetric(rawPpr.computed_p50) ? Number(rawPpr.computed_p50) : null,
        computed_p90: isFiniteMetric(rawPpr.computed_p90) ? Number(rawPpr.computed_p90) : null,
        source: rawPpr.source || "vllm_histogram",
      };
    }

    const wins = (cs && cs.windows) || {};
    const fullWin = wins.full || wins.profiling || {};
    const inDist = fullWin.input_tokens;
    const reqCount = fullWin.request_count ?? fullWin.total_requests;
    const meanPrompt = statVal(inDist, "mean")
      ?? (tokenFlow && isFiniteMetric(reqCount) && Number(reqCount) > 0 ? tokenFlow.sent_tokens / Number(reqCount) : null);
    if (!isFiniteMetric(meanPrompt) || Number(meanPrompt) <= 0) return null;

    const meanVal = Number(meanPrompt);
    const cachedPct = (tokenFlow && isFiniteMetric(tokenFlow.cached_pct))
      ? clampPct(tokenFlow.cached_pct)
      : (hitPctFromSummary(summary) !== null
          ? clampPct(hitPctFromSummary(summary))
          : (isFiniteMetric(derived && derived.avoided_prefill_ratio) ? clampPct(derived.avoided_prefill_ratio) : 0));
    const meanCached = meanVal * (cachedPct / 100);
    const meanComputed = Math.max(0, meanVal - meanCached);
    return {
      mean_prompt_tokens: meanVal,
      mean_cached_tokens: meanCached,
      mean_computed_tokens: meanComputed,
      cached_pct: cachedPct,
      prompt_p50: statVal(inDist, "p50"),
      prompt_p90: statVal(inDist, "p90"),
      computed_p50: null,
      computed_p90: null,
      source: "client_fallback",
    };
  }

  function deriveRoleTokenSourcesData(st, cs, srvPts, rawRoleSeries, summary, derived, cleanedPods, durationSec, stepSec, isDisagg) {
    const rawRows = derived && derived.prompt_tokens_by_role_source;
    if (Array.isArray(rawRows) && rawRows.length) {
      const normalized = rawRows
        .filter((row) => {
          if (!row || typeof row !== "object") return false;
          const r = String(row.role || "").trim().toLowerCase();
          return r && r !== "epp" && r !== "router" && r !== "runner" && r !== "client";
        })
        .map((row) => {
          const lc = Math.max(0, Number(row.local_cache_tokens || 0));
          const ek = Math.max(0, Number(row.external_kv_tokens || 0));
          const fc = Math.max(0, Number(row.local_compute_tokens || 0));
          const total = isFiniteMetric(row.total_tokens) && Number(row.total_tokens) > 0 ? Number(row.total_tokens) : (lc + ek + fc);
          return {
            role: String(row.role),
            local_cache_tokens: lc,
            external_kv_tokens: ek,
            local_compute_tokens: fc,
            total_tokens: total,
          };
        })
        .filter((row) => row.total_tokens > 0);
      if (normalized.length) return normalized;
    }

    const rows = [];
    const rolePts = timedSeriesPoints(rawRoleSeries, st, cs);
    const roleSum = (derived && derived.role_summary) || {};
    if (isDisagg) {
      for (const [key, label] of [["prefill", "Prefill"], ["decode", "Decode"]]) {
        const rs = roleSum[key] || {};
        const lc = integrateRatePoints(rolePts, `${key}_engine_prompt_local_cache_tps`, stepSec, rs.engine_prompt_local_cache_tps, durationSec);
        const ek = integrateRatePoints(rolePts, `${key}_engine_prompt_external_tps`, stepSec, rs.engine_prompt_external_tps, durationSec);
        const fc = integrateRatePoints(rolePts, `${key}_engine_prompt_local_compute_tps`, stepSec, rs.engine_prompt_local_compute_tps, durationSec);
        if (lc !== null || ek !== null || fc !== null) {
          const total = (lc || 0) + (ek || 0) + (fc || 0);
          if (total > 0) {
            rows.push({
              role: label,
              local_cache_tokens: lc || 0,
              external_kv_tokens: ek || 0,
              local_compute_tokens: fc || 0,
              total_tokens: total,
            });
          }
        }
      }
      if (rows.length) return rows;
    }

    if (Array.isArray(cleanedPods) && cleanedPods.length && isFiniteMetric(durationSec) && Number(durationSec) > 0) {
      const byRole = new Map();
      for (const pod of cleanedPods) {
        const rawRole = effectiveReportPodRole(pod);
        if (rawRole === "epp" || rawRole === "runner") continue;
        const label = rawRole === "prefill" ? "Prefill" : (rawRole === "decode" ? "Decode" : "Worker");
        const lc = podStatMean(pod, "engine_prompt_local_cache_tps");
        const ek = podStatMean(pod, "engine_prompt_external_tps");
        const fc = podStatMean(pod, "engine_prompt_local_compute_tps");
        if (lc === null && ek === null && fc === null) continue;
        const cur = byRole.get(label) || { role: label, local_cache_tokens: 0, external_kv_tokens: 0, local_compute_tokens: 0 };
        cur.local_cache_tokens += Math.max(0, (lc || 0) * Number(durationSec));
        cur.external_kv_tokens += Math.max(0, (ek || 0) * Number(durationSec));
        cur.local_compute_tokens += Math.max(0, (fc || 0) * Number(durationSec));
        byRole.set(label, cur);
      }
      for (const cur of byRole.values()) {
        const lc = Math.round(cur.local_cache_tokens);
        const ek = Math.round(cur.external_kv_tokens);
        const fc = Math.round(cur.local_compute_tokens);
        const total = lc + ek + fc;
        if (total > 0) {
          rows.push({ role: cur.role, local_cache_tokens: lc, external_kv_tokens: ek, local_compute_tokens: fc, total_tokens: total });
        }
      }
      if (rows.length) return rows;
    }

    const lcPool = integrateRatePoints(srvPts, "engine_prompt_local_cache_tps", stepSec, podStatMean(summary, "engine_prompt_local_cache_tps"), durationSec);
    const ekPool = integrateRatePoints(srvPts, "engine_prompt_external_tps", stepSec, podStatMean(summary, "engine_prompt_external_tps"), durationSec);
    const fcPool = integrateRatePoints(srvPts, "engine_prompt_local_compute_tps", stepSec, podStatMean(summary, "engine_prompt_local_compute_tps"), durationSec);
    if (lcPool !== null || ekPool !== null || fcPool !== null) {
      const total = (lcPool || 0) + (ekPool || 0) + (fcPool || 0);
      if (total > 0) {
        rows.push({
          role: isDisagg ? "Prefill" : "Worker",
          local_cache_tokens: lcPool || 0,
          external_kv_tokens: ekPool || 0,
          local_compute_tokens: fcPool || 0,
          total_tokens: total,
          _pool_fallback: true,
        });
      }
    }
    return rows;
  }

  function resolveRawTopologyMode(st, d) {
    return String(
      (st && st.topology_mode)
      || (st && st.topology && st.topology.mode)
      || (st && st.topology)
      || (d && d.run_manifest && d.run_manifest.target && d.run_manifest.target.topology_mode)
      || "aggregated"
    ).toLowerCase();
  }

  function hasPrefillTelemetryEvidence(st) {
    if (!st || typeof st !== "object") return false;
    const perPod = Array.isArray(st.per_pod) ? st.per_pod : [];
    const rawRoleSeries = Array.isArray(st.role_series) ? st.role_series : [];
    const derived = st.derived || {};
    const sbr = st.summary_by_role || {};
    const roleSummary = derived.role_summary || {};
    if (perPod.some((p) => effectiveReportPodRole(p) === "prefill")) return true;
    if (rawRoleSeries.some((rp) => rp && typeof rp === "object" && Object.keys(rp).some((k) => k.startsWith("prefill_") && isFiniteMetric(rp[k])))) return true;
    if (sbr.prefill && typeof sbr.prefill === "object" && Object.keys(sbr.prefill).length > 0) return true;
    if (roleSummary.prefill && typeof roleSummary.prefill === "object" && Object.keys(roleSummary.prefill).length > 0) return true;
    if (
      Array.isArray(derived.prompt_tokens_by_role_source)
      && derived.prompt_tokens_by_role_source.some((r) => String((r && r.role) || "").trim().toLowerCase() === "prefill" && Number((r && r.total_tokens) || 0) > 0)
    ) {
      return true;
    }
    return false;
  }

  function isDisaggregatedReport(st, d) {
    return resolveRawTopologyMode(st, d) === "disaggregated" && hasPrefillTelemetryEvidence(st);
  }

  function buildRoleSeriesLookup(rawRoleSeries) {
    const roles = Array.isArray(rawRoleSeries) ? rawRoleSeries : [];
    const roleByTs = new Map();
    for (const rp of roles) {
      if (rp && typeof rp.timestamp === "string" && rp.timestamp) {
        roleByTs.set(rp.timestamp, rp);
      }
    }
    return {
      roles,
      roleByTs,
      at(pt, idx) {
        if (pt && typeof pt.timestamp === "string" && roleByTs.has(pt.timestamp)) {
          return roleByTs.get(pt.timestamp);
        }
        return roles[idx] || {};
      },
    };
  }

  function resolveRoleSourceMetrics(roleSourcesData) {
    const rows = Array.isArray(roleSourcesData) ? roleSourcesData : [];
    const prefillRoleRow = rows.find((r) => !r._pool_fallback && String((r && r.role) || "").trim().toLowerCase() === "prefill" && Number((r && r.total_tokens) || 0) > 0) || null;
    const decodeRoleRow = rows.find((r) => !r._pool_fallback && String((r && r.role) || "").trim().toLowerCase() === "decode" && Number((r && r.total_tokens) || 0) > 0) || null;
    const prefillAvoidedRatio = prefillRoleRow
      ? Math.min(1, Math.max(0, (Number(prefillRoleRow.local_cache_tokens || 0) + Number(prefillRoleRow.external_kv_tokens || 0)) / Number(prefillRoleRow.total_tokens)))
      : null;
    const prefillAvoidedPct = prefillAvoidedRatio !== null ? prefillAvoidedRatio * 100 : null;
    let prefillLocalCacheShare = 1.0;
    if (prefillRoleRow && decodeRoleRow) {
      const pLc = Math.max(0, Number(prefillRoleRow.local_cache_tokens || 0));
      const dLc = Math.max(0, Number(decodeRoleRow.local_cache_tokens || 0));
      if (pLc + dLc > 0) prefillLocalCacheShare = pLc / (pLc + dLc);
    }
    return { prefillRoleRow, decodeRoleRow, prefillAvoidedRatio, prefillAvoidedPct, prefillLocalCacheShare };
  }

  function meanFiniteList(vals) {
    const finite = (vals || []).filter((v) => isFiniteMetric(v)).map(Number);
    return finite.length ? finite.reduce((a, b) => a + b, 0) / finite.length : null;
  }

  function maxFiniteList(vals) {
    const finite = (vals || []).filter((v) => isFiniteMetric(v)).map(Number);
    return finite.length ? Math.max(...finite) : null;
  }

  function meanOfRoleSeries(roleSeries, key) {
    return meanFiniteList((roleSeries || []).map((rp) => (rp ? rp[key] : null)));
  }

  function maxOfRoleSeries(roleSeries, key) {
    return maxFiniteList((roleSeries || []).map((rp) => (rp ? rp[key] : null)));
  }

  function stripEndpointRank(ep) {
    return String(ep || "").replace(/-rank-\d+$/, "");
  }

  function matchEndpointPod(epName, knownPods) {
    const raw = String(epName || "");
    if (!raw) return "";
    if (knownPods.includes(raw)) return raw;
    const stripped = stripEndpointRank(raw);
    if (!knownPods.length || knownPods.includes(stripped)) return stripped;
    let best = null;
    for (const pod of knownPods) {
      if (raw.startsWith(`${pod}-`) && (!best || pod.length > best.length)) {
        best = pod;
      }
    }
    return best || stripped;
  }

  function buildPodRoleMap(cleanedPods) {
    const map = new Map();
    for (const p of cleanedPods || []) {
      const name = p && (p.pod || p.name);
      if (name) map.set(String(name), effectiveReportPodRole(p));
    }
    return map;
  }

  function inferEndpointRole(row, podRoleMap) {
    const explicit = String((row && row.role) || "").trim().toLowerCase();
    if (explicit === "prefill" || explicit === "decode" || explicit === "worker") return explicit;
    const rawName = String((row && (row.pod || row.endpoint)) || "");
    const stripped = stripEndpointRank(rawName);
    if (podRoleMap && podRoleMap.has(rawName)) return podRoleMap.get(rawName);
    if (podRoleMap && podRoleMap.has(stripped)) return podRoleMap.get(stripped);
    if (podRoleMap) {
      let bestPod = null;
      for (const pod of podRoleMap.keys()) {
        if (rawName.startsWith(`${pod}-`) && (!bestPod || pod.length > bestPod.length)) bestPod = pod;
      }
      if (bestPod) return podRoleMap.get(bestPod);
    }
    if (/prefill/i.test(rawName)) return "prefill";
    if (/decode/i.test(rawName)) return "decode";
    return "worker";
  }

  function buildInflightByPod(perEndpoint, knownPods) {
    const byPod = new Map();
    for (const ep of perEndpoint || []) {
      if (!ep || typeof ep !== "object") continue;
      const epName = String(ep.endpoint || ep.pod || "");
      const load = ep.inflight_token_load && typeof ep.inflight_token_load === "object"
        ? firstFinite(ep.inflight_token_load.mean)
        : firstFinite(ep.inflight_token_load);
      if (!epName || load === null) continue;
      const pod = matchEndpointPod(epName, knownPods);
      byPod.set(pod, (byPod.get(pod) || 0) + load);
    }
    return byPod;
  }

  function resolveEndpointDistributionRows(st, derived, cleanedPods, perEndpoint, isDisaggregated) {
    const drv = derived || {};
    const pods = Array.isArray(cleanedPods) ? cleanedPods : [];
    const eps = Array.isArray(perEndpoint) ? perEndpoint : [];
    const knownPods = pods.map((p) => String(p && (p.pod || p.name) || "")).filter(Boolean);
    const podRoleMap = buildPodRoleMap(pods);
    const inflightByPod = buildInflightByPod(eps, knownPods);

    const joinInflight = (row) => {
      const existing = row.inflight_token_load && typeof row.inflight_token_load === "object"
        ? firstFinite(row.inflight_token_load.mean)
        : firstFinite(row.inflight_token_load);
      if (existing !== null) return existing;
      const rawName = String(row.pod || row.endpoint || "");
      const matched = matchEndpointPod(rawName, knownPods);
      if (inflightByPod.has(matched)) return inflightByPod.get(matched);
      if (inflightByPod.has(rawName)) return inflightByPod.get(rawName);
      return null;
    };

    if (!isDisaggregated) {
      const baseRows = Array.isArray(drv.request_destination_distribution) && drv.request_destination_distribution.length
        ? drv.request_destination_distribution
        : (Array.isArray(drv.decode_endpoint_distribution) && drv.decode_endpoint_distribution.length
            ? drv.decode_endpoint_distribution
            : eps);
      return baseRows.map((r) => ({
        ...r,
        role: inferEndpointRole(r, podRoleMap),
        percent: firstFinite(r.percent, r.share_percent),
        inflight_token_load: joinInflight(r),
      }));
    }

    const pDist = Array.isArray(drv.prefill_endpoint_distribution) ? drv.prefill_endpoint_distribution : [];
    const dDist = Array.isArray(drv.decode_endpoint_distribution) ? drv.decode_endpoint_distribution : [];
    const rDist = Array.isArray(drv.request_destination_distribution) ? drv.request_destination_distribution : [];
    let rawRows;
    if (pDist.length || dDist.length) {
      rawRows = pDist.map((r) => ({ ...r, role: r.role || "prefill" }))
        .concat(dDist.map((r) => ({ ...r, role: r.role || "decode" })));
    } else if (rDist.length) {
      rawRows = rDist.map((r) => ({ ...r, role: inferEndpointRole(r, podRoleMap) }));
    } else {
      rawRows = eps.map((r) => ({ ...r, role: inferEndpointRole(r, podRoleMap) }));
    }

    const tagged = rawRows.map((r) => ({
      ...r,
      role: inferEndpointRole(r, podRoleMap),
      inflight_token_load: joinInflight(r),
    }));

    const pools = new Map();
    for (const r of tagged) {
      const rk = String(r.role || "worker").toLowerCase();
      if (!pools.has(rk)) pools.set(rk, []);
      pools.get(rk).push(r);
    }
    const poolMeta = new Map();
    for (const [rk, items] of pools.entries()) {
      const rateSum = items.reduce((acc, item) => {
        const v = firstFinite(item.request_rate_rps);
        return v !== null && v > 0 ? acc + v : acc;
      }, 0);
      const pctSum = items.reduce((acc, item) => {
        const v = firstFinite(item.percent, item.share_percent);
        return v !== null && v > 0 ? acc + v : acc;
      }, 0);
      poolMeta.set(rk, { rateSum, pctSum });
    }

    return tagged.map((r) => {
      const rk = String(r.role || "worker").toLowerCase();
      const meta = poolMeta.get(rk) || { rateSum: 0, pctSum: 0 };
      const rate = firstFinite(r.request_rate_rps);
      const rawPct = firstFinite(r.percent, r.share_percent);
      let normPct = null;
      if (meta.rateSum > 0 && rate !== null && rate >= 0) {
        normPct = (rate / meta.rateSum) * 100;
      } else if (meta.pctSum > 0 && rawPct !== null && rawPct >= 0) {
        normPct = (rawPct / meta.pctSum) * 100;
      }
      return {
        ...r,
        percent: normPct,
      };
    });
  }

  function coefficientOfVariation(values) {
    const finite = (values || []).filter((v) => isFiniteMetric(v)).map(Number);
    if (finite.length < 2) return finite.length ? 0.0 : null;
    const mean = finite.reduce((a, b) => a + b, 0) / finite.length;
    if (!(mean > 0)) return null;
    const variance = finite.reduce((acc, v) => acc + (v - mean) ** 2, 0) / finite.length;
    return Math.round((Math.sqrt(variance) / mean) * 1e5) / 1e5;
  }

  function resolveEffectiveTokenLoadCv(derived, perEndpoint, perPod, isDisaggregated) {
    const eps = Array.isArray(perEndpoint) ? perEndpoint : [];
    const pods = (Array.isArray(perPod) ? perPod : []).filter((p) => effectiveReportPodRole(p) !== "runner");
    const knownPods = pods.map((p) => String(p && (p.pod || p.name) || "")).filter(Boolean);
    const podRoleMap = buildPodRoleMap(pods);
    const inflightByPod = buildInflightByPod(eps, knownPods);

    if (!isDisaggregated) {
      const existing = firstFinite(derived && derived.token_load_cv);
      if (existing !== null) return existing;
      return coefficientOfVariation(Array.from(inflightByPod.values()));
    }

    if (inflightByPod.size > 0) {
      const buckets = new Map();
      for (const [pod, load] of inflightByPod.entries()) {
        const rk = inferEndpointRole({ pod }, podRoleMap);
        if (rk === "epp" || rk === "runner") continue;
        if (!buckets.has(rk)) buckets.set(rk, []);
        buckets.get(rk).push(load);
      }
      const roleCvs = [];
      let hasAnyLoad = false;
      for (const loads of buckets.values()) {
        if (loads.length > 0) hasAnyLoad = true;
        if (loads.length >= 2) {
          const cv = coefficientOfVariation(loads);
          if (cv !== null) roleCvs.push(cv);
        }
      }
      if (roleCvs.length > 0) return Math.max(...roleCvs);
      if (hasAnyLoad) return 0.0;
    }
    return firstFinite(derived && derived.token_load_cv);
  }

  function resolveEffectiveKvMetrics(st, srvPts, roleLookup, cleanedPods, isDisaggregated) {
    const summary = (st && st.summary) || {};
    const sbr = (st && st.summary_by_role) || {};
    const roleSum = (st && st.derived && st.derived.role_summary) || {};
    const metricSources = (st && st.metric_sources) || {};
    const isPdCanonical = metricSources.pd_canonical_metrics === "role_separated";

    const decodeKvPoint = (pt, idx) => {
      const rp = roleLookup.at(pt, idx);
      return clampPct(rp.decode_kv_cache_usage_max_percent ?? rp.decode_kv_cache_usage_percent);
    };
    const prefillKvPoint = (pt, idx) => {
      const rp = roleLookup.at(pt, idx);
      return clampPct(rp.prefill_kv_cache_usage_max_percent ?? rp.prefill_kv_cache_usage_percent);
    };
    const hasDecodeKv = isDisaggregated && srvPts.some((p, i) => decodeKvPoint(p, i) !== null);
    const hasPrefillKv = isDisaggregated && srvPts.some((p, i) => prefillKvPoint(p, i) !== null);
    const effectiveKvPoint = (pt, idx) => {
      const rawKv = clampPct(isFiniteMetric(pt.kv_cache_usage_percent) ? pt.kv_cache_usage_percent : pt.gpu_cache_usage_percent);
      if (!isDisaggregated) return rawKv;
      const dKv = decodeKvPoint(pt, idx);
      const pKv = prefillKvPoint(pt, idx);
      const vals = [dKv, pKv].filter((v) => v !== null);
      return vals.length ? Math.max(...vals) : rawKv;
    };

    if (!isDisaggregated) {
      return {
        decodeKvPoint,
        prefillKvPoint,
        effectiveKvPoint,
        hasDecodeKv: false,
        hasPrefillKv: false,
        kvPeak: (summary.kv_cache_usage_percent && summary.kv_cache_usage_percent.max) ?? summary.peak_kv_cache_pct,
        kvMean: podStatMean(summary, "kv_cache_usage_percent"),
      };
    }

    const decodeSeries = srvPts.map((p, i) => decodeKvPoint(p, i));
    const prefillSeries = srvPts.map((p, i) => prefillKvPoint(p, i));
    const effectiveSeries = srvPts.map((p, i) => effectiveKvPoint(p, i));

    const podPeaks = (cleanedPods || [])
      .filter((p) => {
        const r = effectiveReportPodRole(p);
        return r === "prefill" || r === "decode";
      })
      .map((p) => statMax(p && p.kv_cache_usage_percent));

    const peakCandidates = [
      sbr.decode && statMax(sbr.decode.kv_cache_usage_max_percent ?? sbr.decode.kv_cache_usage_percent),
      sbr.prefill && statMax(sbr.prefill.kv_cache_usage_max_percent ?? sbr.prefill.kv_cache_usage_percent),
      maxFiniteList(decodeSeries),
      maxFiniteList(prefillSeries),
      ...podPeaks,
    ].filter((v) => isFiniteMetric(v)).map(Number);

    const kvPeak = peakCandidates.length
      ? Math.max(...peakCandidates)
      : ((summary.kv_cache_usage_percent && summary.kv_cache_usage_percent.max) ?? summary.peak_kv_cache_pct);

    const roleSummaryKvMax = maxFiniteList([
      roleSum.decode && roleSum.decode.kv_cache_usage_percent,
      roleSum.prefill && roleSum.prefill.kv_cache_usage_percent,
    ]);
    const kvMean = isPdCanonical
      ? podStatMean(summary, "kv_cache_usage_percent")
      : (meanFiniteList(effectiveSeries) ?? roleSummaryKvMax ?? podStatMean(summary, "kv_cache_usage_percent"));

    return { decodeKvPoint, prefillKvPoint, effectiveKvPoint, hasDecodeKv, hasPrefillKv, kvPeak, kvMean };
  }

  function resolveEffectivePrefixHitMetrics(st, roleSourcesData, isDisaggregated) {
    const summary = (st && st.summary) || {};
    const sbr = (st && st.summary_by_role) || {};
    const roleSeries = Array.isArray(st && st.role_series) ? st.role_series : [];
    const roleSum = (st && st.derived && st.derived.role_summary) || {};
    const metricSources = (st && st.metric_sources) || {};
    const isPdCanonical = metricSources.pd_canonical_metrics === "role_separated";

    if (!isDisaggregated) {
      return {
        hitAvg: hitPctFromSummary(summary),
        hitMax: summary.prefix_cache_hit_percent && summary.prefix_cache_hit_percent.max,
      };
    }

    const { prefillAvoidedPct } = resolveRoleSourceMetrics(roleSourcesData);
    const hitAvg = podStatMean(sbr.prefill, "prefix_cache_hit_percent")
      ?? meanOfRoleSeries(roleSeries, "prefill_prefix_cache_hit_percent")
      ?? (isPdCanonical ? hitPctFromSummary(summary) : prefillAvoidedPct)
      ?? firstFinite(roleSum.prefill && roleSum.prefill.prefix_cache_hit_percent)
      ?? hitPctFromSummary(summary);

    const hitMax = (sbr.prefill && statMax(sbr.prefill.prefix_cache_hit_percent))
      ?? maxOfRoleSeries(roleSeries, "prefill_prefix_cache_hit_percent")
      ?? (summary.prefix_cache_hit_percent && summary.prefix_cache_hit_percent.max);

    return { hitAvg, hitMax };
  }

  const SATURATION_STATES = ["healthy", "queueing", "kv_pressure", "evicting", "error"];
  const SATURATION_STATE_META = {
    healthy: { label: "Healthy", color: STATUS_COLORS.healthy, badgeClass: "good", rank: 0 },
    queueing: { label: "Queueing", color: STATUS_COLORS.queueing, badgeClass: "warn", rank: 1 },
    kv_pressure: { label: "KV Pressure", color: STATUS_COLORS.kv_pressure, badgeClass: "pressure", rank: 2 },
    evicting: { label: "Evicting", color: STATUS_COLORS.evicting, badgeClass: "crit", rank: 3 },
    error: { label: "Error", color: STATUS_COLORS.error, badgeClass: "crit", rank: 4 },
    unknown: { label: "Unknown", color: STATUS_COLORS.unknown, badgeClass: "", rank: -1 },
  };
  const ENGINE_ROLE_ORDER = ["prefill", "decode", "worker", "workers"];

  function normalizeSaturationStatus(raw) {
    const key = String(raw || "unknown").toLowerCase().trim();
    return SATURATION_STATE_META[key] ? key : "unknown";
  }

  function formatRoleTitle(role) {
    const raw = String(role || "worker").trim();
    const low = raw.toLowerCase();
    if (low === "prefill") return "Prefill";
    if (low === "decode") return "Decode";
    if (low === "worker") return "Worker";
    if (low === "workers") return "Workers";
    if (low === "epp") return "EPP Router";
    if (low === "client") return "Client";
    return raw.charAt(0).toUpperCase() + raw.slice(1);
  }

  function formatPreemptions(v) {
    if (!isFiniteMetric(v)) return "N/A";
    return fmtDec(Math.max(0, Number(v)));
  }

  function formatPctChip(val) {
    if (!Number.isFinite(val) || val <= 0) return "0%";
    return pct(val);
  }

  function formatSecRange(start, end) {
    const s = Number(start || 0);
    const e = Number(end || 0);
    return `${fmtDec(s)}–${fmtDec(e)}s`;
  }

  function normalizeTimelineBuckets(raw) {
    if (!Array.isArray(raw) || !raw.length) return [];
    const first = raw.find((item) => item && typeof item === "object");
    if (!first) return [];
    if (first.roles && typeof first.roles === "object") {
      return raw
        .filter((b) => b && typeof b === "object" && b.roles && typeof b.roles === "object")
        .map((b, idx) => {
          const normRoles = {};
          for (const [role, info] of Object.entries(b.roles)) {
            if (!info || typeof info !== "object") continue;
            normRoles[role] = {
              status: normalizeSaturationStatus(info.status ?? info.state),
              running: info.running ?? null,
              waiting: info.waiting ?? null,
              kv_cache_pct: info.kv_cache_pct ?? info.kv_pct ?? null,
              prefix_hit_pct: info.prefix_hit_pct ?? null,
              preemptions: info.preemptions ?? 0,
              failed_requests: info.failed_requests ?? info.failed ?? 0,
            };
          }
          const bIdx = isFiniteMetric(b.bucket_index) ? Number(b.bucket_index) : idx;
          const startS = isFiniteMetric(b.start_seconds) ? Number(b.start_seconds) : bIdx * 5;
          const endS = isFiniteMetric(b.end_seconds) ? Math.max(startS, Number(b.end_seconds)) : startS + 5;
          return {
            bucket_index: bIdx,
            start_seconds: startS,
            end_seconds: endS,
            roles: normRoles,
          };
        })
        .sort((a, b) => (a.bucket_index - b.bucket_index) || (a.start_seconds - b.start_seconds));
    }

    const byIdx = new Map();
    raw.forEach((cell, idx) => {
      if (!cell || typeof cell !== "object") return;
      const bIdx = isFiniteMetric(cell.bucket_index) ? Number(cell.bucket_index) : idx;
      const key = cell.bucket_index !== undefined && cell.bucket_index !== null
        ? String(cell.bucket_index)
        : `${isFiniteMetric(cell.start_seconds) ? Number(cell.start_seconds) : idx}:${isFiniteMetric(cell.end_seconds) ? Number(cell.end_seconds) : idx}`;
      if (!byIdx.has(key)) {
        const startS = isFiniteMetric(cell.start_seconds) ? Number(cell.start_seconds) : bIdx * 5;
        const endS = isFiniteMetric(cell.end_seconds) ? Math.max(startS, Number(cell.end_seconds)) : startS + 5;
        byIdx.set(key, {
          bucket_index: bIdx,
          start_seconds: startS,
          end_seconds: endS,
          roles: {},
        });
      }
      const entry = byIdx.get(key);
      const role = cell.role || "worker";
      entry.roles[role] = {
        status: normalizeSaturationStatus(cell.state ?? cell.status),
        running: cell.running ?? null,
        waiting: cell.waiting ?? null,
        kv_cache_pct: cell.kv_cache_pct ?? cell.kv_pct ?? null,
        prefix_hit_pct: cell.prefix_hit_pct ?? null,
        preemptions: cell.preemptions ?? 0,
        failed_requests: cell.failed_requests ?? cell.failed ?? 0,
      };
    });
    return Array.from(byIdx.values()).sort((a, b) => (a.bucket_index - b.bucket_index) || (a.start_seconds - b.start_seconds));
  }

  function summarizeEppHealth(buckets, eppRole) {
    let totalFailed = 0;
    let errBuckets = 0;
    for (const b of buckets) {
      const info = (b.roles || {})[eppRole];
      if (!info) continue;
      const failed = isFiniteMetric(info.failed_requests) ? Math.max(0, Number(info.failed_requests)) : 0;
      totalFailed += failed;
      if (normalizeSaturationStatus(info.status ?? info.state) === "error" || failed > 0) {
        errBuckets += 1;
      }
    }
    if (totalFailed > 0) {
      return el("span", { className: "badge crit", "data-epp-health": "failed", textContent: `EPP Router: ${intFmt(totalFailed)} failed` });
    }
    if (errBuckets > 0) {
      return el("span", { className: "badge crit", "data-epp-health": "error", textContent: `EPP Router: ${intFmt(errBuckets)} error` });
    }
    return el("span", { className: "badge good", "data-epp-health": "healthy", textContent: "EPP Router: Healthy" });
  }

  function RoleSaturationChartSVG(container, role, buckets, opts) {
    const config = opts || {};
    const warmupEnd = isFiniteMetric(config.warmupEnd) ? Number(config.warmupEnd) : 0;
    const drainStart = isFiniteMetric(config.drainStart) ? Number(config.drainStart) : null;
    const roleBuckets = (buckets || []).filter((b) => b && b.roles && Object.prototype.hasOwnProperty.call(b.roles, role) && b.roles[role]);
    const rows = roleBuckets.map((b) => {
      const start = isFiniteMetric(b.start_seconds) ? Number(b.start_seconds) : 0;
      const end = isFiniteMetric(b.end_seconds) ? Math.max(start, Number(b.end_seconds)) : start;
      const mid = end > start ? (start + end) / 2 : start;
      const info = b.roles[role];
      const status = normalizeSaturationStatus(info.status ?? info.state);
      return {
        start,
        end,
        mid,
        status,
        running: isFiniteMetric(info.running) ? Math.max(0, Number(info.running)) : null,
        waiting: isFiniteMetric(info.waiting) ? Math.max(0, Number(info.waiting)) : null,
        kv_cache_pct: clampPct(info.kv_cache_pct ?? info.kv_pct),
        prefix_hit_pct: clampPct(info.prefix_hit_pct),
        preemptions: isFiniteMetric(info.preemptions) ? Math.max(0, Number(info.preemptions)) : 0,
        failed_requests: isFiniteMetric(info.failed_requests) ? Math.max(0, Number(info.failed_requests)) : 0,
      };
    });

    const rowStarts = rows.map((r) => r.start);
    const rowEnds = rows.map((r) => r.end);
    const minX = isFiniteMetric(config.minX) ? Number(config.minX) : Math.min(0, ...rowStarts);
    const maxX = Math.max(minX + 1, isFiniteMetric(config.maxX) ? Number(config.maxX) : 0, ...rowEnds);
    const reqVals = [];
    for (const r of rows) {
      if (r.running !== null) reqVals.push(r.running);
      if (r.waiting !== null) reqVals.push(r.waiting);
    }
    const rawMaxReq = reqVals.length ? Math.max(...reqVals) : 0;
    const minL = 0;
    const maxL = rawMaxReq > 0 ? Math.max(1, rawMaxReq * 1.15) : 4;
    const minR = 0;
    const maxR = 100;

    const height = config.height || 220;
    const pad = phaseBoundaryPad(54, warmupEnd, drainStart, minX, maxX, "full");
    const ih = height - pad.t - pad.b;

    const syL = (y) => pad.t + ih - ((Math.max(minL, y) - minL) / (maxL - minL)) * ih;
    const syR = (y) => pad.t + ih - ((Math.min(maxR, Math.max(minR, y)) - minR) / (maxR - minR)) * ih;

    return mountResponsiveChart(container, { minWidth: 280, fallbackWidth: 560 }, (width) => {
      const iw = width - pad.l - pad.r;
      const sx = (x) => pad.l + ((x - minX) / (maxX - minX)) * iw;
      const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height, style: "display:block" });
    const gridColor = cssVar("--border");
    const muted = cssVar("--text-muted");
    const cardBg = cssVar("--bg-card") || "#171f2c";

    // Shaded saturation zone rectangles for non-healthy buckets behind curves
    for (const r of rows) {
      if (r.status === "healthy" || r.status === "unknown") continue;
      const x1 = sx(r.start);
      const x2 = sx(r.end);
      const zoneW = Math.max(2, x2 - x1);
      const zoneColor = STATUS_COLORS[r.status] || STATUS_COLORS.unknown;
      svg.appendChild(svgEl("rect", {
        x: x1.toFixed(1),
        y: pad.t,
        width: zoneW.toFixed(1),
        height: ih,
        fill: zoneColor,
        opacity: 0.14,
        "data-saturation-zone": r.status,
      }));
    }

    renderPhaseBoundaries(svg, {
      width,
      pad,
      iw,
      ih,
      minX,
      maxX,
      sx,
      muted,
      warmupEnd,
      drainStart,
      windowMode: "full",
    });

    // Left Y-axis gridlines & ticks (Requests)
    for (let i = 0; i <= 4; i++) {
      const yVal = minL + ((maxL - minL) * i) / 4;
      const y = syL(yVal);
      svg.appendChild(svgEl("line", { x1: pad.l, y1: y.toFixed(1), x2: pad.l + iw, y2: y.toFixed(1), stroke: gridColor, "stroke-width": 0.8 }));
      svg.appendChild(svgEl("text", { x: pad.l - 6, y: (y + 4).toFixed(1), fill: muted, "font-size": 10, "text-anchor": "end" }, [document.createTextNode(axisNum(yVal, maxL))]));
    }
    svg.appendChild(svgEl("text", {
      transform: `translate(12 ${(pad.t + ih / 2).toFixed(1)}) rotate(-90)`,
      fill: muted,
      "font-size": 10,
      "text-anchor": "middle",
    }, [document.createTextNode("Requests")]));

    // Right Y-axis ticks (KV Cache %)
    for (const kvTick of [0, 50, 100]) {
      const y = syR(kvTick);
      svg.appendChild(svgEl("text", { x: pad.l + iw + 6, y: (y + 4).toFixed(1), fill: muted, "font-size": 10, "text-anchor": "start" }, [document.createTextNode(`${kvTick}%`)]));
    }
    svg.appendChild(svgEl("text", {
      transform: `translate(${(width - 8).toFixed(1)} ${(pad.t + ih / 2).toFixed(1)}) rotate(-90)`,
      fill: muted,
      "font-size": 10,
      "text-anchor": "middle",
    }, [document.createTextNode("KV Cache %")]));

    // X-axis ticks (Elapsed seconds)
    for (let i = 0; i <= 4; i++) {
      const xVal = minX + ((maxX - minX) * i) / 4;
      const x = sx(xVal);
      svg.appendChild(svgEl("text", { x: x.toFixed(1), y: height - 8, fill: muted, "font-size": 10, "text-anchor": "middle" }, [document.createTextNode(Math.round(xVal) + "s")]));
    }

    // Horizontal dashed KV 90% threshold reference line on right axis
    const kv90Y = syR(90);
    svg.appendChild(svgEl("line", {
      x1: pad.l,
      y1: kv90Y.toFixed(1),
      x2: pad.l + iw,
      y2: kv90Y.toFixed(1),
      stroke: STATUS_COLORS.kv_pressure,
      "stroke-dasharray": "5 4",
      "stroke-width": 1.3,
      "data-ref-line": "kv-90-threshold",
    }));
    svg.appendChild(svgEl("text", {
      x: pad.l + iw - 4,
      y: (kv90Y + 11).toFixed(1),
      fill: STATUS_COLORS.kv_pressure,
      "font-size": 10,
      "font-weight": 600,
      "text-anchor": "end",
    }, [document.createTextNode("KV 90%")]));

    function drawSeries(pts, syFn, color, withArea) {
      const segments = [];
      let seg = [];
      for (const pt of pts) {
        if (isFiniteMetric(pt.y)) {
          seg.push({ x: Number(pt.x), y: Number(pt.y) });
        } else if (seg.length) {
          segments.push(seg);
          seg = [];
        }
      }
      if (seg.length) segments.push(seg);
      const baseY = syFn(0);
      for (const s of segments) {
        if (s.length > 1) {
          const lineD = s.map((p, idx) => `${idx === 0 ? "M" : "L"}${sx(p.x).toFixed(1)},${syFn(p.y).toFixed(1)}`).join(" ");
          if (withArea) {
            const areaD = `${lineD} L${sx(s[s.length - 1].x).toFixed(1)},${baseY.toFixed(1)} L${sx(s[0].x).toFixed(1)},${baseY.toFixed(1)} Z`;
            svg.appendChild(svgEl("path", { d: areaD, fill: color, "fill-opacity": 0.16, stroke: "none" }));
          }
          svg.appendChild(svgEl("path", { d: lineD, fill: "none", stroke: color, "stroke-width": 2 }));
        } else {
          const p = s[0];
          const px = sx(p.x);
          const py = syFn(p.y);
          if (withArea && p.y > 0) {
            svg.appendChild(svgEl("rect", {
              x: (px - 5).toFixed(1),
              y: py.toFixed(1),
              width: 10,
              height: Math.max(1, baseY - py).toFixed(1),
              fill: color,
              "fill-opacity": 0.16,
              stroke: "none",
            }));
          }
          svg.appendChild(svgEl("circle", {
            cx: px.toFixed(1),
            cy: py.toFixed(1),
            r: 3.5,
            fill: color,
            stroke: cardBg,
            "stroke-width": 1.5,
          }));
        }
      }
    }

    drawSeries(rows.map((r) => ({ x: r.mid, y: r.running })), syL, PALETTE[0], true);
    drawSeries(rows.map((r) => ({ x: r.mid, y: r.waiting })), syL, PALETTE[1], true);
    drawSeries(rows.map((r) => ({ x: r.mid, y: r.kv_cache_pct })), syR, PALETTE[2], false);

    const crosshair = svgEl("line", {
      x1: pad.l,
      y1: pad.t,
      x2: pad.l,
      y2: pad.t + ih,
      stroke: cssVar("--accent"),
      "stroke-width": 1,
      "stroke-dasharray": "3 3",
      visibility: "hidden",
    });
    svg.appendChild(crosshair);

    const overlay = svgEl("rect", { x: pad.l, y: pad.t, width: iw, height: ih, fill: "transparent", style: "cursor:crosshair" });
    overlay.addEventListener("mousemove", (evt) => {
      const rect = svg.getBoundingClientRect();
      if (!rect || !rect.width || !rows.length) return;
      const relX = ((evt.clientX - rect.left) / rect.width) * width;
      const ratio = Math.min(1, Math.max(0, (relX - pad.l) / iw));
      const t = minX + ratio * (maxX - minX);
      let active = rows.find((r) => t >= r.start && t <= r.end);
      if (!active) {
        active = rows[0];
        let bestDist = Infinity;
        for (const r of rows) {
          const d = Math.abs(r.mid - t);
          if (d < bestDist) {
            bestDist = d;
            active = r;
          }
        }
      }
      const cx = sx(active.mid);
      crosshair.setAttribute("x1", cx.toFixed(1));
      crosshair.setAttribute("x2", cx.toFixed(1));
      crosshair.setAttribute("visibility", "visible");

      const meta = SATURATION_STATE_META[active.status] || SATURATION_STATE_META.unknown;
      const tipLines = [
        `${formatRoleTitle(role)} · ${formatSecRange(active.start, active.end)}`,
        `Status: ${meta.label}`,
        `Running: ${num(active.running, 1)} | Waiting: ${num(active.waiting, 1)}`,
        `KV Cache: ${pct(active.kv_cache_pct)} | Prefix Cache Hit Rate: ${pct(active.prefix_hit_pct)}`,
        `Preemptions: ${formatPreemptions(active.preemptions)} | Failed: ${intFmt(active.failed_requests)}`,
      ];
      showTip(evt, tipLines.join("\n"));
    });
    overlay.addEventListener("mouseleave", () => {
      crosshair.setAttribute("visibility", "hidden");
      hideTip();
    });
    svg.appendChild(overlay);

    const legend = el("div", { className: "chart-legend" }, [
      el("span", { className: "legend-item" }, [el("span", { className: "legend-swatch", style: `background:${PALETTE[0]}` }), "Running Requests"]),
      el("span", { className: "legend-item" }, [el("span", { className: "legend-swatch", style: `background:${PALETTE[1]}` }), "Waiting Requests"]),
      el("span", { className: "legend-item" }, [el("span", { className: "legend-swatch", style: `background:${PALETTE[2]}` }), "KV Cache Usage (%)"]),
      el("span", { className: "legend-item" }, [
        el("span", {
          className: "legend-swatch",
          style: `width:14px;height:0;border-top:2px dashed ${STATUS_COLORS.kv_pressure};background:transparent;border-radius:0`,
        }),
        "KV 90% Threshold",
      ]),
      ...SATURATION_STATES.filter((stKey) => stKey !== "healthy").map((stKey) => {
        const meta = SATURATION_STATE_META[stKey];
        return el("span", { className: "legend-item" }, [
          el("span", {
            className: "legend-swatch",
            style: `background:${meta.color};opacity:0.45;border:1px solid ${meta.color}`,
          }),
          meta.label,
        ]);
      }),
    ]);

    container.replaceChildren(svg, legend);
    });
  }

  function buildRoleSaturationCard(role, buckets, opts) {
    const counts = { healthy: 0, queueing: 0, kv_pressure: 0, evicting: 0, error: 0 };
    let worstStatus = "healthy";
    let worstRank = -1;
    let peakWaiting = null;
    let peakKv = null;
    let totalPreemptions = 0;
    let totalFailed = 0;

    const roleBuckets = (buckets || []).filter((b) => b && b.roles && Object.prototype.hasOwnProperty.call(b.roles, role) && b.roles[role]);
    for (const b of roleBuckets) {
      const info = b.roles[role];
      const stKey = normalizeSaturationStatus(info.status ?? info.state);
      if (counts[stKey] !== undefined) counts[stKey] += 1;
      const meta = SATURATION_STATE_META[stKey] || SATURATION_STATE_META.unknown;
      if (meta.rank > worstRank) {
        worstRank = meta.rank;
        worstStatus = stKey;
      }
      if (isFiniteMetric(info.waiting)) {
        const w = Math.max(0, Number(info.waiting));
        peakWaiting = peakWaiting === null ? w : Math.max(peakWaiting, w);
      }
      const kvVal = clampPct(info.kv_cache_pct ?? info.kv_pct);
      if (kvVal !== null) {
        peakKv = peakKv === null ? kvVal : Math.max(peakKv, kvVal);
      }
      if (isFiniteMetric(info.preemptions)) {
        totalPreemptions += Math.max(0, Number(info.preemptions));
      }
      if (isFiniteMetric(info.failed_requests)) {
        totalFailed += Math.max(0, Number(info.failed_requests));
      }
    }

    const worstMeta = SATURATION_STATE_META[worstStatus] || SATURATION_STATE_META.healthy;
    const knownTotal = SATURATION_STATES.reduce((acc, stKey) => acc + (counts[stKey] || 0), 0);
    const totalBuckets = knownTotal > 0 ? knownTotal : Math.max(1, roleBuckets.length);

    const kpiItems = [
      el("span", null, ["Peak Waiting: ", el("strong", { textContent: peakWaiting !== null ? `${num(peakWaiting, 1)} reqs` : "N/A" })]),
      el("span", null, ["Peak KV Cache: ", el("strong", { textContent: peakKv !== null ? pct(peakKv) : "N/A" })]),
      el("span", null, ["Preemptions: ", el("strong", { textContent: formatPreemptions(totalPreemptions) })]),
    ];
    if (totalFailed > 0) {
      kpiItems.push(el("span", null, ["Failed: ", el("strong", { textContent: intFmt(totalFailed) })]));
    }

    const segBar = el(
      "div",
      { className: "sat-state-bar", role: "img", "aria-label": `${formatRoleTitle(role)} time-in-state distribution` },
      SATURATION_STATES.map((stKey) => {
        const count = counts[stKey] || 0;
        if (count <= 0) return null;
        const meta = SATURATION_STATE_META[stKey];
        const sharePct = (count / totalBuckets) * 100;
        const seg = el("div", {
          className: "sat-state-seg",
          style: `width:${sharePct.toFixed(2)}%;background:${meta.color}`,
          "data-state": stKey,
        });
        seg.addEventListener("mousemove", (evt) => {
          showTip(evt, `${formatRoleTitle(role)} · ${meta.label}\n${intFmt(count)} / ${intFmt(totalBuckets)} buckets (${pct(sharePct)})`);
        });
        seg.addEventListener("mouseleave", hideTip);
        return seg;
      }).filter(Boolean)
    );

    const chips = el(
      "div",
      { className: "sat-state-chips" },
      SATURATION_STATES.map((stKey) => {
        const count = counts[stKey] || 0;
        const meta = SATURATION_STATE_META[stKey];
        const sharePct = (count / totalBuckets) * 100;
        return el("span", { className: `sat-state-chip${count === 0 ? " zero" : ""}`, "data-state-chip": stKey }, [
          el("span", { className: "sat-state-dot", style: `background:${meta.color}` }),
          el("span", { textContent: meta.label }),
          el("span", { textContent: formatPctChip(sharePct) }),
        ]);
      })
    );

    const chartHost = el("div", { className: "sat-chart-wrap" });
    RoleSaturationChartSVG(chartHost, role, roleBuckets, opts);

    return el("div", { className: "sat-role-card", "data-role": role }, [
      el("div", { className: "sat-role-head" }, [
        el("span", { className: "sat-role-title", textContent: formatRoleTitle(role) }),
        el("span", { className: `badge ${worstMeta.badgeClass}`.trim(), "data-role-status": worstStatus, textContent: worstMeta.label }),
      ]),
      el("div", { className: "sat-role-kpis" }, kpiItems),
      el("div", { className: "sat-state-bar-wrap" }, [segBar, chips]),
      chartHost,
    ]);
  }

  function renderClientOnlyTimeline(container, buckets, clientRole, opts) {
    const config = opts || {};
    const warmupEnd = isFiniteMetric(config.warmupEnd) ? Number(config.warmupEnd) : 0;
    const drainStart = isFiniteMetric(config.drainStart) ? Number(config.drainStart) : null;
    let totalFailed = 0;
    let errBuckets = 0;
    let healthyBuckets = 0;
    const clientBuckets = (buckets || []).filter((b) => b && b.roles && Object.prototype.hasOwnProperty.call(b.roles, clientRole) && b.roles[clientRole]);
    const rows = clientBuckets.map((b) => {
      const info = b.roles[clientRole];
      const failed = isFiniteMetric(info.failed_requests) ? Math.max(0, Number(info.failed_requests)) : 0;
      const stKey = normalizeSaturationStatus(info.status ?? info.state);
      const isErr = stKey === "error" || failed > 0;
      totalFailed += failed;
      if (isErr) errBuckets += 1;
      else healthyBuckets += 1;
      const start = isFiniteMetric(b.start_seconds) ? Number(b.start_seconds) : 0;
      const end = isFiniteMetric(b.end_seconds) ? Math.max(start, Number(b.end_seconds)) : start;
      return {
        start,
        end,
        status: isErr ? "error" : "healthy",
        failed,
      };
    });

    const notice = el("div", { className: "sat-fallback-notice", "data-client-fallback": "true" }, [
      el("div", { className: "title-row" }, [
        el("span", { className: "badge info", textContent: "Client-Only Fallback" }),
        el("strong", { textContent: "Serving Engine Telemetry Unavailable" }),
      ]),
      el("div", {
        className: "section-sub",
        textContent: "Prometheus engine metrics were not collected for this run, so per-role running/waiting requests and KV cache usage cannot be plotted. Showing client-side request error buckets instead.",
      }),
      el("div", { className: "sat-role-kpis" }, [
        el("span", null, ["Failed Requests: ", el("strong", { textContent: intFmt(totalFailed) })]),
        el("span", null, ["Error Buckets: ", el("strong", { textContent: `${intFmt(errBuckets)} / ${intFmt(rows.length)}` })]),
        el("span", null, ["Healthy Buckets: ", el("strong", { textContent: `${intFmt(healthyBuckets)} / ${intFmt(rows.length)}` })]),
      ]),
    ]);

    const minX = Math.min(0, ...rows.map((r) => r.start));
    const maxX = Math.max(minX + 1, ...rows.map((r) => r.end));
    const height = 84;
    const pad = phaseBoundaryPad(18, warmupEnd, drainStart, minX, maxX, "full");
    pad.l = 18;
    const ih = height - pad.t - pad.b;

    const chartHost = el("div", { className: "sat-chart-wrap" });
    const card = el("div", { className: "sat-role-card", "data-role": clientRole }, [chartHost]);
    container.replaceChildren(notice, card);

    return mountResponsiveChart(chartHost, { minWidth: 280, fallbackWidth: 640 }, (width) => {
      const iw = width - pad.l - pad.r;
      const sx = (x) => pad.l + ((x - minX) / (maxX - minX)) * iw;
      const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height, style: "display:block" });
      const muted = cssVar("--text-muted");

      renderPhaseBoundaries(svg, {
        width,
        pad,
        iw,
        ih,
        minX,
        maxX,
        sx,
        muted,
        warmupEnd,
        drainStart,
        windowMode: "full",
      });

      rows.forEach((r) => {
        const x1 = sx(r.start);
        const x2 = sx(r.end);
        const cellW = Math.max(3, x2 - x1 - 2);
        const color = r.status === "error" ? STATUS_COLORS.error : STATUS_COLORS.healthy;
        const rect = svgEl("rect", {
          x: (x1 + 1).toFixed(1),
          y: (pad.t + 4).toFixed(1),
          width: cellW.toFixed(1),
          height: Math.max(8, ih - 8).toFixed(1),
          rx: 3,
          fill: color,
          opacity: 0.85,
        });
        rect.addEventListener("mousemove", (evt) => {
          showTip(evt, `Client · ${formatSecRange(r.start, r.end)}\nStatus: ${r.status === "error" ? "Error" : "Healthy"}\nFailed Requests: ${intFmt(r.failed)}`);
        });
        rect.addEventListener("mouseleave", hideTip);
        svg.appendChild(rect);
      });

      for (let i = 0; i <= 4; i++) {
        const xVal = minX + ((maxX - minX) * i) / 4;
        svg.appendChild(svgEl("text", { x: sx(xVal).toFixed(1), y: height - 8, fill: muted, "font-size": 10, "text-anchor": "middle" }, [document.createTextNode(Math.round(xVal) + "s")]));
      }

      const legend = el("div", { className: "chart-legend" }, [
        el("span", { className: "legend-item" }, [el("span", { className: "legend-swatch", style: `background:${STATUS_COLORS.healthy}` }), "Healthy (0 failed)"]),
        el("span", { className: "legend-item" }, [el("span", { className: "legend-swatch", style: `background:${STATUS_COLORS.error}` }), "Error (failed > 0)"]),
      ]);

      chartHost.replaceChildren(svg, legend);
    });
  }

  function ForensicsTimelineSVG(container, rawBuckets, opts) {
    cleanupChartMountsIn(container);
    const config = opts || {};
    const buckets = normalizeTimelineBuckets(rawBuckets);
    const controlsHost = config.controlsHost || null;
    if (controlsHost) controlsHost.replaceChildren();

    if (!buckets.length) {
      container.replaceChildren(el("div", { className: "empty-note", textContent: "Per-role saturation timeline unavailable for this run." }));
      return;
    }

    const roleSet = new Set();
    for (const b of buckets) {
      for (const r of Object.keys(b.roles || {})) roleSet.add(r);
    }
    const allRoles = Array.from(roleSet);
    if (!allRoles.length) {
      container.replaceChildren(el("div", { className: "empty-note", textContent: "No role status buckets recorded." }));
      return;
    }

    const eppRole = allRoles.find((r) => r.toLowerCase() === "epp") || null;
    const clientRole = allRoles.find((r) => r.toLowerCase() === "client" || r.toLowerCase() === "runner") || null;
    const engineRoles = allRoles
      .filter((r) => {
        const low = r.toLowerCase();
        return low !== "epp" && low !== "client" && low !== "runner";
      })
      .sort((a, b) => {
        const ai = ENGINE_ROLE_ORDER.indexOf(a.toLowerCase());
        const bi = ENGINE_ROLE_ORDER.indexOf(b.toLowerCase());
        if (ai !== -1 && bi !== -1) return ai - bi;
        if (ai !== -1) return -1;
        if (bi !== -1) return 1;
        return a.localeCompare(b);
      });

    const minX = Math.min(0, ...buckets.map((b) => (isFiniteMetric(b.start_seconds) ? Number(b.start_seconds) : 0)));
    const maxX = Math.max(minX + 1, ...buckets.map((b) => (isFiniteMetric(b.end_seconds) ? Number(b.end_seconds) : 0)));
    const boundaries = state.data ? resolveRunPhaseBoundaries(state.data) : { warmupEnd: 0, drainStart: null };
    const chartOpts = {
      warmupEnd: config.warmupEnd !== undefined ? config.warmupEnd : boundaries.warmupEnd,
      drainStart: config.drainStart !== undefined ? config.drainStart : boundaries.drainStart,
      minX,
      maxX,
    };

    if (engineRoles.length) {
      const outNodes = [];
      if (eppRole) {
        if (controlsHost) controlsHost.appendChild(summarizeEppHealth(buckets, eppRole));
        else outNodes.push(el("div", { className: "stage-controls" }, [summarizeEppHealth(buckets, eppRole)]));
      }
      const cards = engineRoles.map((role) => buildRoleSaturationCard(role, buckets, chartOpts));
      if (cards.length >= 2) {
        outNodes.push(el("div", { className: "grid-2" }, cards));
      } else {
        outNodes.push(cards[0]);
      }
      container.replaceChildren(...outNodes);
      return;
    }

    if (clientRole) {
      renderClientOnlyTimeline(container, buckets, clientRole, chartOpts);
      return;
    }

    if (eppRole) {
      if (controlsHost) controlsHost.appendChild(summarizeEppHealth(buckets, eppRole));
      container.replaceChildren(
        el("div", { className: "sat-fallback-notice", "data-epp-only": "true" }, [
          el("div", { className: "title-row" }, [summarizeEppHealth(buckets, eppRole)]),
          el("div", { className: "section-sub", textContent: "No serving engine role saturation buckets recorded for this run." }),
        ])
      );
      return;
    }

    container.replaceChildren(el("div", { className: "empty-note", textContent: "No role status buckets recorded." }));
  }

  function decodeCacheClass(raw) {
    if (raw === 2 || raw === "warm") return "warm";
    if (raw === 1 || raw === "partial") return "partial";
    if (raw === 0 || raw === "cold") return "cold";
    return "unknown";
  }

  function classifySlotTier(cacheHitPct) {
    const pctVal = Number(cacheHitPct || 0);
    if (pctVal >= 70) return "warm";
    if (pctVal >= 10) return "partial";
    return "cold";
  }

  function greedyPackSlotTurns(rawItems) {
    const valid = [];
    for (const raw of rawItems || []) {
      if (!raw) continue;
      const a = Number(raw.start_s);
      const b = Number(raw.end_s);
      if (!Number.isFinite(a) || !Number.isFinite(b) || a < 0 || b < a) continue;
      const pctVal = Number.isFinite(Number(raw.cache_hit_pct)) ? Math.min(100, Math.max(0, Number(raw.cache_hit_pct))) : 0;
      const tier = raw.tier === "warm" || raw.tier === "partial" || raw.tier === "cold" ? raw.tier : classifySlotTier(pctVal);
      valid.push({
        start_s: a,
        end_s: b,
        cache_hit_pct: pctVal,
        tier,
        session_id: raw.session_id ? String(raw.session_id) : null,
        turn: raw.turn != null && Number.isFinite(Number(raw.turn)) ? Number(raw.turn) : null,
        input_tokens: raw.input_tokens != null && Number.isFinite(Number(raw.input_tokens)) ? Math.round(Number(raw.input_tokens)) : null,
        output_tokens: raw.output_tokens != null && Number.isFinite(Number(raw.output_tokens)) ? Math.round(Number(raw.output_tokens)) : null,
        ttft_ms: raw.ttft_ms != null && Number.isFinite(Number(raw.ttft_ms)) ? Number(raw.ttft_ms) : null,
      });
    }
    valid.sort((x, y) => (x.start_s - y.start_s) || (x.end_s - y.end_s));
    const slotFreeAt = [];
    const tierCounts = { cold: 0, partial: 0, warm: 0 };
    const sessionIds = new Set();
    const packed = [];
    for (const req of valid) {
      let slotIdx = -1;
      for (let k = 0; k < slotFreeAt.length; k++) {
        if (slotFreeAt[k] <= req.start_s) {
          slotIdx = k;
          slotFreeAt[k] = req.end_s;
          break;
        }
      }
      if (slotIdx < 0) {
        slotIdx = slotFreeAt.length;
        slotFreeAt.push(req.end_s);
      }
      tierCounts[req.tier] += 1;
      if (req.session_id) sessionIds.add(req.session_id);
      packed.push({ ...req, slot: slotIdx });
    }
    const totalRequests = packed.length;
    const sampleStride = totalRequests > 2000 ? Math.ceil(totalRequests / 2000) : 1;
    const items = sampleStride > 1 ? packed.filter((_item, idx) => idx % sampleStride === 0) : packed;
    return {
      peakSlots: slotFreeAt.length,
      totalRequests,
      sessionCount: sessionIds.size,
      sampleStride,
      tierCounts,
      items,
    };
  }

  function resolveSlotLanesData(clientSeries, sessionsSummary) {
    const sl = clientSeries && clientSeries.slot_lanes;
    if (sl && Array.isArray(sl.items) && sl.items.length > 0) {
      const counts = sl.tier_counts || {};
      return {
        peakSlots: Number(sl.peak_slots || 0),
        totalRequests: Number(sl.total_requests ?? sl.items.length),
        sessionCount: Number(sl.session_count || 0),
        sampleStride: Math.max(1, Number(sl.sample_stride || 1)),
        tierCounts: {
          cold: Number(counts.cold || 0),
          partial: Number(counts.partial || 0),
          warm: Number(counts.warm || 0),
        },
        items: sl.items,
      };
    }

    const ss = sessionsSummary || {};
    if (Array.isArray(ss.sessions) && ss.sessions.length) {
      const turns = [];
      for (const sess of ss.sessions) {
        if (!sess) continue;
        const sid = sess.session_id || sess.chat_id || sess.id || null;
        const rawTurns = Array.isArray(sess.turns) ? sess.turns : (Array.isArray(sess.requests) ? sess.requests : [sess]);
        rawTurns.forEach((t, idx) => {
          if (!t) return;
          const startS = Number(t.start_s ?? t.arrived_s ?? t.arrived_rel);
          const endS = Number(t.end_s ?? t.completed_s ?? t.completed_rel ?? startS);
          let hitPct = 0;
          if (isFiniteMetric(t.cache_hit_pct)) {
            hitPct = Math.min(100, Math.max(0, Number(t.cache_hit_pct)));
          } else if (isFiniteMetric(t.cached_tokens) && isFiniteMetric(t.input_tokens) && Number(t.input_tokens) > 0) {
            hitPct = Math.min(100, Math.max(0, (Number(t.cached_tokens) / Number(t.input_tokens)) * 100));
          } else if (isFiniteMetric(t.overlap)) {
            hitPct = Math.min(100, Math.max(0, Number(t.overlap) * 100));
          } else if (t.tier === "warm" || t.cache_cls === 2) {
            hitPct = 100;
          } else if (t.tier === "partial" || t.cache_cls === 1) {
            hitPct = 40;
          }
          turns.push({
            start_s: startS,
            end_s: endS,
            cache_hit_pct: hitPct,
            tier: classifySlotTier(hitPct),
            session_id: sid || t.session_id || null,
            turn: t.turn ?? (idx + 1),
            input_tokens: t.input_tokens,
            output_tokens: t.output_tokens,
            ttft_ms: t.ttft_ms,
          });
        });
      }
      return greedyPackSlotTurns(turns);
    }

    if (ss.available && Array.isArray(ss.lanes) && ss.lanes.length) {
      const sessionIds = Array.isArray(ss.session_ids) ? ss.session_ids : [];
      const laneTurns = ss.lanes.map((lane) => {
        const [idIdx, startS, endS, rawCls, ttftMs] = lane;
        const cls = decodeCacheClass(rawCls);
        const hitPct = cls === "warm" ? 100 : (cls === "partial" ? 40 : 0);
        return {
          start_s: Number(startS || 0),
          end_s: Number(endS ?? startS ?? 0),
          cache_hit_pct: hitPct,
          tier: classifySlotTier(hitPct),
          session_id: sessionIds[idIdx] || `#${idIdx}`,
          turn: null,
          input_tokens: null,
          output_tokens: null,
          ttft_ms: ttftMs,
        };
      });
      return greedyPackSlotTurns(laneTurns);
    }

    return null;
  }

  function SwimlaneCanvas(container, clientSeries, sessionsSummary, warmupSeconds) {
    const slotData = resolveSlotLanesData(clientSeries, sessionsSummary);
    if (!slotData || !slotData.items.length || slotData.peakSlots <= 0) {
      cleanupChartContainer(container);
      const reason = (sessionsSummary && sessionsSummary.reason) || "No concurrent request slot data available.";
      container.replaceChildren(el("div", { className: "empty-note", textContent: reason }));
      return null;
    }

    const TIER_COLORS = {
      cold: "#f85149",
      partial: "#f59e0b",
      warm: "#10b981",
    };
    const effWarmup = Math.max(0, Number(warmupSeconds || 0) || 0);

    const sessionTurnCounts = new Map();
    for (const it of slotData.items) {
      const sid = it && it.session_id != null ? String(it.session_id).trim() : "";
      if (sid) {
        sessionTurnCounts.set(sid, (sessionTurnCounts.get(sid) || 0) + 1);
      }
    }

    let selectedSessionId = null;
    let currentWidth = 720;

    const legendItems = [
      el("span", { className: "legend-item" }, [el("span", { className: "legend-swatch", style: `background:${TIER_COLORS.cold}` }), "cold (<10%)"]),
      el("span", { className: "legend-item" }, [el("span", { className: "legend-swatch", style: `background:${TIER_COLORS.partial}` }), "partial (10–70%)"]),
      el("span", { className: "legend-item" }, [el("span", { className: "legend-swatch", style: `background:${TIER_COLORS.warm}` }), "warm (≥70%)"]),
    ];
    if (effWarmup > 0) {
      legendItems.push(
        el("span", { className: "legend-item" }, [
          el("span", { className: "legend-swatch", style: "background:rgba(245, 158, 11, 0.18);border:1px dashed #f59e0b" }),
          "warmup",
        ])
      );
    }
    const summaryText = `${intFmt(slotData.peakSlots)} slots at peak · ${intFmt(slotData.totalRequests)} requests · ${intFmt(slotData.sessionCount)} sessions`
      + (slotData.sampleStride > 1 ? ` · 1 in ${intFmt(slotData.sampleStride)} sampled` : "");

    const peakSlots = Math.max(1, slotData.peakSlots, ...slotData.items.map((it) => Number(it.slot || 0) + 1));
    const targetPlotH = 200;
    const minRowH = 8;
    const ih = Math.max(targetPlotH, peakSlots * minRowH);
    const rowH = ih / peakSlots;
    const rowGap = rowH >= 10 ? 3 : (rowH >= 7 ? 2 : 1);
    const barH = Math.min(18, Math.max(2, rowH - rowGap));
    const pad = { l: effWarmup > 0 ? 52 : 44, r: 16, t: effWarmup > 0 ? 20 : 10, b: 24 };
    const height = pad.t + ih + pad.b;

    const maxItemT = Math.max(0, ...slotData.items.map((it) => Number(it.end_s || 0)));
    const maxT = Math.max(0.1, Number((clientSeries && clientSeries.duration_seconds) || 0), effWarmup, maxItemT);

    const buildTooltipText = (it, slotIdx, startS, endS, tier, sid) => {
      const durMs = Math.max(0, (endS - startS) * 1000);
      const pctText = isFiniteMetric(it.cache_hit_pct) ? pct(it.cache_hit_pct) : "N/A";
      const totalTurns = sid ? (sessionTurnCounts.get(sid) || 1) : 0;
      const turnSuffix = it.turn != null
        ? ` · Turn ${it.turn}${totalTurns > 1 ? ` of ${intFmt(totalTurns)}` : ""}`
        : (totalTurns > 1 ? ` · ${intFmt(totalTurns)} turns` : "");
      const sessLine = `Session: ${sid || "N/A"}${turnSuffix}`;
      const tokLine = `Tokens: ${it.input_tokens != null ? intFmt(it.input_tokens) : "N/A"} prompt / ${it.output_tokens != null ? intFmt(it.output_tokens) : "N/A"} output`;
      const lines = [
        `Slot S${slotIdx + 1}`,
        sessLine,
        `${fmtDec(startS)}s – ${fmtDec(endS)}s (${msFmt(durMs)})`,
        `Cache Hit: ${pctText} (${tier})`,
        tokLine,
        `TTFT: ${it.ttft_ms != null ? msFmt(it.ttft_ms) : "N/A"}`,
      ];
      if (totalTurns > 1) {
        lines.push(selectedSessionId === sid ? "Click again to clear highlight" : `Click to highlight all ${intFmt(totalTurns)} turns`);
      } else if (sid && selectedSessionId === sid) {
        lines.push("Click again to clear highlight");
      }
      return lines.join("\n");
    };

    const renderSwimlane = (width, opts) => {
      currentWidth = width;
      const iw = width - pad.l - pad.r;
      const sx = (t) => pad.l + (Math.max(0, Math.min(maxT, t)) / maxT) * iw;

      const gridColor = cssVar("--border");
      const muted = cssVar("--text-muted");
      const textColor = cssVar("--text");
      const cardBg = cssVar("--bg-card");
      const hasSelection = selectedSessionId !== null;

      const legend = el("div", { className: "chart-legend", style: "margin-top:0" }, legendItems);
      const summaryNode = el("span", { className: "section-sub mono", textContent: summaryText });
      const rightNodes = [];
      const activeSessionTurns = [];

      const svg = svgEl("svg", {
        viewBox: `0 0 ${width} ${height.toFixed(1)}`,
        width: "100%",
        height: height.toFixed(1),
        tabindex: "0",
        style: "display:block;outline:none;",
      });

      svg.addEventListener("click", () => {
        if (selectedSessionId !== null) {
          selectedSessionId = null;
          hideTip();
          renderSwimlane(currentWidth);
        }
      });
      svg.addEventListener("keydown", (evt) => {
        if (evt && evt.key === "Escape" && selectedSessionId !== null) {
          if (typeof evt.stopPropagation === "function") evt.stopPropagation();
          selectedSessionId = null;
          hideTip();
          renderSwimlane(currentWidth);
        }
      });

      svg.appendChild(svgEl("rect", {
        x: pad.l,
        y: pad.t,
        width: iw,
        height: ih.toFixed(2),
        fill: "none",
        stroke: gridColor,
        "stroke-width": 0.6,
        opacity: 0.6,
      }));

      if (rowH >= 6) {
        for (let s = 0; s < peakSlots; s++) {
          const ry = pad.t + s * rowH;
          if (s % 2 === 1) {
            svg.appendChild(svgEl("rect", { x: pad.l, y: ry.toFixed(2), width: iw, height: rowH.toFixed(2), fill: "rgba(148, 163, 184, 0.04)" }));
          }
          svg.appendChild(svgEl("line", { x1: pad.l, y1: ry.toFixed(2), x2: pad.l + iw, y2: ry.toFixed(2), stroke: gridColor, "stroke-width": 0.4, opacity: 0.45 }));
        }
        svg.appendChild(svgEl("line", { x1: pad.l, y1: (pad.t + ih).toFixed(2), x2: pad.l + iw, y2: (pad.t + ih).toFixed(2), stroke: gridColor, "stroke-width": 0.6 }));
      }

      let swimWarmupX = null;
      if (effWarmup > 0) {
        const wx = sx(Math.min(effWarmup, maxT));
        swimWarmupX = wx;
        svg.appendChild(svgEl("rect", { x: pad.l, y: pad.t, width: Math.max(0, wx - pad.l).toFixed(2), height: ih.toFixed(2), fill: "rgba(245, 158, 11, 0.12)" }));
        svg.appendChild(svgEl("line", { x1: wx.toFixed(2), y1: pad.t, x2: wx.toFixed(2), y2: (pad.t + ih).toFixed(2), stroke: "#f59e0b", "stroke-dasharray": "4 3", "stroke-width": 1.2 }));
      }

      for (let i = 0; i <= 4; i++) {
        const tVal = (maxT * i) / 4;
        const gx = sx(tVal);
        svg.appendChild(svgEl("line", { x1: gx.toFixed(2), y1: pad.t, x2: gx.toFixed(2), y2: (pad.t + ih).toFixed(2), stroke: gridColor, "stroke-width": 0.6, opacity: 0.6 }));
        const tLabel = maxT < 10 ? fmtDec(tVal) + "s" : Math.round(tVal) + "s";
        svg.appendChild(svgEl("text", { x: gx.toFixed(2), y: (height - 7).toFixed(1), fill: muted, "font-size": 10, "text-anchor": "middle" }, [document.createTextNode(tLabel)]));
      }

      const slotLabelIndices = new Set([0, peakSlots - 1]);
      if (peakSlots <= 12) {
        for (let s = 0; s < peakSlots; s++) slotLabelIndices.add(s);
      } else {
        const step = Math.max(1, Math.ceil((peakSlots - 1) / 7));
        for (let s = 0; s < peakSlots; s += step) {
          if (s === 0 || (peakSlots - 1 - s) * rowH >= 12) slotLabelIndices.add(s);
        }
      }
      for (const s of Array.from(slotLabelIndices).sort((a, b) => a - b)) {
        const ly = pad.t + s * rowH + rowH / 2 + 3;
        svg.appendChild(svgEl("text", { x: pad.l - 6, y: ly.toFixed(1), fill: muted, "font-size": 9.5, "text-anchor": "end" }, [document.createTextNode(`S${s + 1}`)]));
      }

      const attachBarHandlers = (rect, tipText, sid, baseFillOpacity, hoverFillOpacity) => {
        rect.addEventListener("mouseenter", (evt) => {
          rect.setAttribute("fill-opacity", hoverFillOpacity);
          showTip(evt, tipText);
        });
        rect.addEventListener("mousemove", (evt) => showTip(evt, tipText));
        rect.addEventListener("mouseleave", () => {
          rect.setAttribute("fill-opacity", baseFillOpacity);
          hideTip();
        });
        rect.addEventListener("click", (evt) => {
          if (evt && typeof evt.stopPropagation === "function") evt.stopPropagation();
          if (!sid) {
            if (selectedSessionId !== null) {
              selectedSessionId = null;
              hideTip();
              renderSwimlane(currentWidth);
            }
            return;
          }
          selectedSessionId = selectedSessionId === sid ? null : sid;
          hideTip();
          renderSwimlane(currentWidth, { focusSvg: selectedSessionId !== null });
        });
      };

      for (const it of slotData.items) {
        const sid = it && it.session_id != null ? String(it.session_id).trim() : "";
        const slotIdx = Math.max(0, Math.min(peakSlots - 1, Number(it.slot || 0)));
        const startS = Math.max(0, Number(it.start_s || 0));
        const endS = Math.max(startS, Number(it.end_s ?? startS));
        const x1 = sx(startS);
        const x2 = sx(endS);
        const y = pad.t + slotIdx * rowH + (rowH - barH) / 2;
        const midY = y + barH / 2;
        const tier = it.tier === "warm" || it.tier === "partial" || it.tier === "cold" ? it.tier : classifySlotTier(it.cache_hit_pct);
        const fill = TIER_COLORS[tier] || TIER_COLORS.cold;
        const tipText = buildTooltipText(it, slotIdx, startS, endS, tier, sid);

        if (hasSelection && sid === selectedSessionId) {
          const selW = Math.max(4, x2 - x1);
          activeSessionTurns.push({
            it,
            sid,
            startS,
            endS,
            x1,
            endX: x1 + selW,
            y,
            barW: selW,
            midY,
            fill,
            tipText,
          });
          continue;
        }

        const barW = Math.max(2, x2 - x1);
        const baseOp = hasSelection ? "0.16" : "0.92";
        const hoverOp = hasSelection ? "0.45" : "1";
        const rect = svgEl("rect", {
          x: x1.toFixed(2),
          y: y.toFixed(2),
          width: barW.toFixed(2),
          height: barH.toFixed(2),
          rx: Math.min(3, barH / 2).toFixed(2),
          fill,
          "fill-opacity": baseOp,
          "data-session-id": sid || null,
          style: "cursor:pointer",
        });
        attachBarHandlers(rect, tipText, sid, baseOp, hoverOp);
        svg.appendChild(rect);
      }

      if (hasSelection) {
        activeSessionTurns.sort((a, b) => {
          if (a.startS !== b.startS) return a.startS - b.startS;
          if (a.endS !== b.endS) return a.endS - b.endS;
          const ta = a.it.turn != null && Number.isFinite(Number(a.it.turn)) ? Number(a.it.turn) : 0;
          const tb = b.it.turn != null && Number.isFinite(Number(b.it.turn)) ? Number(b.it.turn) : 0;
          return ta - tb;
        });

        if (activeSessionTurns.length >= 2) {
          const connGroup = svgEl("g", {
            "data-session-connectors": selectedSessionId,
            "pointer-events": "none",
          });
          for (let i = 0; i < activeSessionTurns.length - 1; i++) {
            const cur = activeSessionTurns[i];
            const nxt = activeSessionTurns[i + 1];
            connGroup.appendChild(svgEl("line", {
              x1: cur.endX.toFixed(2),
              y1: cur.midY.toFixed(2),
              x2: nxt.x1.toFixed(2),
              y2: nxt.midY.toFixed(2),
              stroke: textColor,
              "stroke-opacity": "0.55",
              "stroke-width": "1.25",
              "stroke-dasharray": "3 3",
              "pointer-events": "none",
            }));
          }
          svg.appendChild(connGroup);
        }

        const selGroup = svgEl("g", { "data-selected-session": selectedSessionId });
        for (const t of activeSessionTurns) {
          const rect = svgEl("rect", {
            x: t.x1.toFixed(2),
            y: t.y.toFixed(2),
            width: t.barW.toFixed(2),
            height: barH.toFixed(2),
            rx: Math.min(3, barH / 2).toFixed(2),
            fill: t.fill,
            "fill-opacity": "1",
            stroke: cardBg,
            "stroke-width": "0.75",
            "data-session-id": t.sid,
            style: "cursor:pointer",
          });
          attachBarHandlers(rect, t.tipText, t.sid, "1", "1");
          const halo = svgEl("rect", {
            x: (t.x1 - 1.5).toFixed(2),
            y: (t.y - 1.5).toFixed(2),
            width: (t.barW + 3).toFixed(2),
            height: (barH + 3).toFixed(2),
            rx: Math.min(4, barH / 2 + 1).toFixed(2),
            fill: "none",
            stroke: textColor,
            "stroke-opacity": "0.95",
            "stroke-width": "1.75",
            "pointer-events": "none",
          });
          selGroup.appendChild(rect);
          selGroup.appendChild(halo);
        }
        svg.appendChild(selGroup);

        const turnCount = activeSessionTurns.length;
        const clearBtn = el("button", {
          type: "button",
          className: "btn",
          style: "padding:1px 7px;font-size:10.5px;border-radius:999px;line-height:1.4;",
          textContent: "Clear",
          onClick: (evt) => {
            if (evt && typeof evt.stopPropagation === "function") evt.stopPropagation();
            if (selectedSessionId !== null) {
              selectedSessionId = null;
              hideTip();
              renderSwimlane(currentWidth);
            }
          },
        });
        const pill = el(
          "span",
          {
            className: "badge info",
            "data-session-pill": selectedSessionId,
            style: "display:inline-flex;align-items:center;gap:6px;padding:2px 8px;border-radius:999px;border:1px solid var(--accent);background:var(--bg-elev);color:var(--text);font-size:11px;font-family:var(--mono);",
          },
          [
            el("span", {
              textContent: `Selected session: ${selectedSessionId} · ${intFmt(turnCount)} ${turnCount === 1 ? "turn" : "turns"}`,
            }),
            clearBtn,
          ]
        );
        rightNodes.push(pill);
      }
      rightNodes.push(summaryNode);

      const rightWrap = el(
        "div",
        { style: "display:flex;align-items:center;flex-wrap:wrap;gap:8px;justify-content:flex-end;" },
        rightNodes
      );
      const topStrip = el(
        "div",
        { style: "display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:8px" },
        [legend, rightWrap]
      );

      if (swimWarmupX !== null) {
        const warmupPos = placeBoundaryLabel(swimWarmupX, BOUNDARY_LABEL_WIDTHS.Warmup, 4, width - 4, "left");
        appendBoundaryLabel(svg, "Warmup", warmupPos, pad.t - 6, muted);
      }

      const svgWrap = el("div", { style: height > 360 ? "max-height:360px;overflow-y:auto;" : "" }, [svg]);
      container.replaceChildren(topStrip, svgWrap);
      if (opts && opts.focusSvg && typeof svg.focus === "function") {
        svg.focus({ preventScroll: true });
      }
    };

    const handle = mountResponsiveChart(container, { minWidth: 300, fallbackWidth: 720 }, (width) => {
      renderSwimlane(width);
    });

    if (container) {
      const onContainerKeydown = (evt) => {
        if (evt && evt.key === "Escape" && selectedSessionId !== null) {
          selectedSessionId = null;
          hideTip();
          renderSwimlane(currentWidth);
        }
      };
      container._swimlaneKeydownHandler = onContainerKeydown;
      container.addEventListener("keydown", onContainerKeydown);
    }

    return handle;
  }

  const HEATMAP_STOPS = [
    [14, 165, 233],  // sky/cyan: rgb(14, 165, 233)
    [16, 185, 129],  // emerald:  rgb(16, 185, 129)
    [245, 158, 11],  // amber:    rgb(245, 158, 11)
    [244, 63, 94],   // rose:     rgb(244, 63, 94)
  ];

  function heatmapColor(ratio) {
    const clamped = Math.max(0, Math.min(1, Number.isFinite(ratio) ? ratio : 0));
    const scaled = clamped * (HEATMAP_STOPS.length - 1);
    const idx = Math.min(Math.floor(scaled), HEATMAP_STOPS.length - 2);
    const t = scaled - idx;
    const a = HEATMAP_STOPS[idx];
    const b = HEATMAP_STOPS[idx + 1];
    const r = Math.round(a[0] + (b[0] - a[0]) * t);
    const g = Math.round(a[1] + (b[1] - a[1]) * t);
    const bl = Math.round(a[2] + (b[2] - a[2]) * t);
    return `rgb(${r}, ${g}, ${bl})`;
  }

  function HeatmapCanvas(container, heatmap, title) {
    cleanupChartContainer(container);

    const isValidNonNeg = (v) =>
      v !== null
      && v !== undefined
      && typeof v !== "boolean"
      && !(typeof v === "string" && v.trim() === "")
      && isFiniteMetric(v)
      && Number(v) >= 0;

    const validPoints = [];
    if (heatmap && Array.isArray(heatmap.points)) {
      for (const p of heatmap.points) {
        if (!p || typeof p !== "object") continue;
        const rawT = p.t ?? p.elapsed_seconds ?? p.time_seconds;
        const rawSeq = p.seq ?? p.sequence_length;
        const rawMs = p.ms ?? p.value ?? p.metric_ms;
        if (!isValidNonNeg(rawT) || !isValidNonNeg(rawSeq) || !isValidNonNeg(rawMs)) continue;
        validPoints.push({
          t: Number(rawT),
          seq: Number(rawSeq),
          ms: Number(rawMs),
        });
      }
    }

    const cells = [];
    if (!validPoints.length && heatmap && Array.isArray(heatmap.cells)) {
      for (const c of heatmap.cells) {
        if (!c || typeof c !== "object") continue;
        const rawStart = c.start_seconds;
        const rawEnd = c.end_seconds;
        const rawSeqStart = c.seq_start ?? c.sequence_length_start;
        const rawSeqEnd = c.seq_end ?? c.sequence_length_end;
        const rawAvg = c.avg_ms ?? c.average_metric_ms;
        if (
          !isFiniteMetric(rawStart)
          || !isFiniteMetric(rawEnd)
          || !isFiniteMetric(rawSeqStart)
          || !isFiniteMetric(rawSeqEnd)
          || !isFiniteMetric(rawAvg)
        ) {
          continue;
        }
        const rawP50 = c.p50_ms ?? c.p50_metric_ms;
        const rawP90 = c.p90_ms ?? c.p90_metric_ms;
        const rawMax = c.max_ms ?? c.maximum_metric_ms;
        const rawCount = c.count ?? c.request_count;
        cells.push({
          start_seconds: Number(rawStart),
          end_seconds: Number(rawEnd),
          seq_start: Number(rawSeqStart),
          seq_end: Number(rawSeqEnd),
          avg_ms: Number(rawAvg),
          p50_ms: isFiniteMetric(rawP50) ? Number(rawP50) : null,
          p90_ms: isFiniteMetric(rawP90) ? Number(rawP90) : null,
          max_ms: isFiniteMetric(rawMax) ? Number(rawMax) : null,
          count: isFiniteMetric(rawCount) ? Math.max(0, Number(rawCount)) : 0,
        });
      }
    }

    if (!validPoints.length && !cells.length) {
      container.replaceChildren(el("div", { className: "empty-note", textContent: `${title} scatter plot unavailable.` }));
      return null;
    }

    const height = 256;
    const pad = { l: 64, r: 16, t: 12, b: 44 };
    const ih = height - pad.t - pad.b;

    const isTpotMetric = String(title || "").toUpperCase().includes("TPOT");
    const metricLabel = String(title || "Latency").trim() || "Latency";
    const unitLabel = isTpotMetric ? "ms/tok" : "ms";
    const valFmt = isTpotMetric ? tpotFmt : msFmt;
    const isInputSeq = (heatmap && heatmap.sequence_length === "input")
      || (!(heatmap && heatmap.sequence_length) && String(title || "").toUpperCase().includes("TTFT"));
    const isRequestedOutput = heatmap && heatmap.sequence_length === "output" && heatmap.sequence_length_source === "requested";
    const seqLabel = isInputSeq
      ? "Input sequence length (tokens)"
      : isRequestedOutput
        ? "Requested output sequence length (tokens)"
        : "Output sequence length (tokens)";
    const seqLabelShort = isInputSeq
      ? "Input sequence length"
      : isRequestedOutput
        ? "Requested output sequence length"
        : "Output sequence length";

    const buildScaleNodes = (prefixText, minV, displayMaxV, metricRange) => {
      const nodes = [
        el("span", { style: "font-family:var(--sans);font-weight:500;color:var(--text);", textContent: prefixText }),
      ];
      if (metricRange <= 0) {
        nodes.push(
          el("span", {
            style: `display:inline-block;width:20px;height:8px;border-radius:4px;border:1px solid var(--border);background:${heatmapColor(0)};`,
            "aria-hidden": "true",
          }),
          el("span", { textContent: `Uniform (${valFmt(minV)})` })
        );
      } else {
        const gradientBar = el("span", {
          style: "position:relative;display:inline-block;width:112px;height:8px;border-radius:4px;overflow:hidden;border:1px solid var(--border);background:linear-gradient(90deg, rgb(14, 165, 233), rgb(16, 185, 129), rgb(245, 158, 11), rgb(244, 63, 94));",
          "aria-hidden": "true",
        }, [
          el("span", { style: "position:absolute;top:0;bottom:0;left:33.333%;width:1px;background:var(--bg-card);opacity:0.65;" }),
          el("span", { style: "position:absolute;top:0;bottom:0;left:66.667%;width:1px;background:var(--bg-card);opacity:0.65;" }),
        ]);
        nodes.push(
          el("span", { textContent: `Low (${valFmt(minV)})` }),
          gradientBar,
          el("span", { textContent: `High (${valFmt(displayMaxV)})` })
        );
      }
      return nodes;
    };

    return mountResponsiveChart(container, { minWidth: 300, fallbackWidth: 560 }, (width) => {
      const iw = width - pad.l - pad.r;
      if (validPoints.length > 0) {
      let minPtT = Infinity;
      let maxPtT = -Infinity;
      let minPtS = Infinity;
      let maxPtS = -Infinity;
      let ptMinV = Infinity;
      let ptMaxV = -Infinity;
      for (const p of validPoints) {
        if (p.t < minPtT) minPtT = p.t;
        if (p.t > maxPtT) maxPtT = p.t;
        if (p.seq < minPtS) minPtS = p.seq;
        if (p.seq > maxPtS) maxPtS = p.seq;
        if (p.ms < ptMinV) ptMinV = p.ms;
        if (p.ms > ptMaxV) ptMaxV = p.ms;
      }

      const winStart = isValidNonNeg(heatmap.window_start_seconds) ? Number(heatmap.window_start_seconds) : 0;
      const durSec = isValidNonNeg(heatmap.duration_seconds) ? Number(heatmap.duration_seconds) : 0;
      const minT = winStart > 0 ? Math.min(winStart, minPtT) : Math.min(0, minPtT);
      const rawMaxT = Math.max(maxPtT, durSec);
      const maxT = rawMaxT > minT ? rawMaxT : minT + 1;

      const padS = maxPtS > minPtS ? Math.max(1, (maxPtS - minPtS) * 0.05) : Math.max(1, Math.round(minPtS * 0.1));
      const minS = Math.max(0, Math.floor(minPtS - padS));
      const rawMaxS = Math.ceil(maxPtS + padS);
      const spanS = Math.max(4, Math.ceil((rawMaxS - minS) / 4) * 4);
      const maxS = minS + spanS;

      const rawMinV = heatmap.min_ms ?? heatmap.minimum_metric_ms;
      const rawMaxV = heatmap.max_ms ?? heatmap.maximum_metric_ms;
      const hasExplicitBounds = isValidNonNeg(rawMinV) && isValidNonNeg(rawMaxV) && Number(rawMinV) <= Number(rawMaxV);
      const minV = hasExplicitBounds ? Math.min(Number(rawMinV), ptMinV) : ptMinV;
      const displayMaxV = hasExplicitBounds ? Math.max(Number(rawMaxV), ptMaxV) : ptMaxV;
      const metricRange = displayMaxV - minV;

      const sx = (t) => pad.l + ((t - minT) / (maxT - minT)) * iw;
      const sy = (s) => pad.t + ih - ((s - minS) / (maxS - minS)) * ih;

      const svg = svgEl("svg", {
        viewBox: `0 0 ${width} ${height}`,
        width: "100%",
        height,
        style: "display:block",
        role: "img",
        "aria-label": `${title} per-request scatter plot by elapsed time and ${seqLabel.toLowerCase()}`,
      });

      const plotBg = svgEl("rect", {
        x: pad.l,
        y: pad.t,
        width: iw,
        height: ih,
        rx: 3,
        fill: "var(--bg)",
        stroke: "var(--border)",
        "stroke-width": 1,
        style: "cursor:crosshair",
      });
      svg.appendChild(plotBg);

      for (let i = 0; i <= 4; i++) {
        const tVal = minT + ((maxT - minT) * i) / 4;
        const gx = sx(tVal);
        if (i > 0 && i < 4) {
          svg.appendChild(svgEl("line", {
            x1: gx.toFixed(2),
            y1: pad.t,
            x2: gx.toFixed(2),
            y2: pad.t + ih,
            stroke: "var(--border)",
            "stroke-dasharray": "3 3",
            "stroke-width": 0.8,
            style: "pointer-events:none",
          }));
        }
        svg.appendChild(svgEl("text", {
          x: gx.toFixed(2),
          y: pad.t + ih + 15,
          fill: "var(--text-muted)",
          "font-size": 10,
          "text-anchor": i === 0 ? "start" : (i === 4 ? "end" : "middle"),
        }, [document.createTextNode(`${fmtDec(tVal)}s`)]));
      }

      for (let i = 0; i <= 4; i++) {
        const sVal = minS + (spanS * i) / 4;
        const gy = sy(sVal);
        if (i > 0 && i < 4) {
          svg.appendChild(svgEl("line", {
            x1: pad.l,
            y1: gy.toFixed(2),
            x2: pad.l + iw,
            y2: gy.toFixed(2),
            stroke: "var(--border)",
            "stroke-dasharray": "3 3",
            "stroke-width": 0.8,
            style: "pointer-events:none",
          }));
        }
        svg.appendChild(svgEl("text", {
          x: pad.l - 7,
          y: (gy + 3.5).toFixed(2),
          fill: "var(--text-muted)",
          "font-size": 10,
          "text-anchor": "end",
        }, [document.createTextNode(intFmt(Math.round(sVal)))]));
      }

      const sortedPoints = validPoints.slice().sort((a, b) => a.ms - b.ms);
      const dotRadius = sortedPoints.length > 800 ? 3.0 : sortedPoints.length > 200 ? 3.5 : 4.2;
      const dotsGroup = svgEl("g");
      const renderedDots = [];

      const hoverRing = svgEl("circle", {
        cx: 0,
        cy: 0,
        r: (dotRadius + 2).toFixed(2),
        fill: "none",
        stroke: "var(--text)",
        "stroke-width": "1.75",
        visibility: "hidden",
        style: "pointer-events:none",
      });

      const activateHover = (pt, cx, cy, evt) => {
        hoverRing.setAttribute("cx", cx.toFixed(2));
        hoverRing.setAttribute("cy", cy.toFixed(2));
        hoverRing.setAttribute("visibility", "visible");
        const tipText = [
          `Elapsed time: ${fmtDec(pt.t)} s`,
          `${seqLabelShort}: ${intFmt(Math.round(pt.seq))} tok`,
          `${metricLabel}: ${valFmt(pt.ms)}`,
        ].join("\n");
        showTip(evt, tipText);
      };

      const clearHover = () => {
        hoverRing.setAttribute("visibility", "hidden");
        hideTip();
      };

      const clientToSvgCoords = (evt) => {
        if (!evt || typeof evt.clientX !== "number" || typeof evt.clientY !== "number") return null;
        if (typeof svg.getScreenCTM === "function") {
          try {
            const ctm = svg.getScreenCTM();
            if (ctm && typeof ctm.inverse === "function") {
              const pt = typeof svg.createSVGPoint === "function"
                ? svg.createSVGPoint()
                : (typeof DOMPoint === "function" ? new DOMPoint() : null);
              if (pt && typeof pt.matrixTransform === "function") {
                pt.x = evt.clientX;
                pt.y = evt.clientY;
                const inv = pt.matrixTransform(ctm.inverse());
                if (Number.isFinite(inv.x) && Number.isFinite(inv.y)) {
                  return { mx: inv.x, my: inv.y };
                }
              }
            }
          } catch {
            // Fall through to bounding rect mapping
          }
        }
        const rect = typeof svg.getBoundingClientRect === "function" ? svg.getBoundingClientRect() : null;
        if (!rect || !rect.width || !rect.height) return null;
        return {
          mx: ((evt.clientX - rect.left) / rect.width) * width,
          my: ((evt.clientY - rect.top) / rect.height) * height,
        };
      };

      for (const pt of sortedPoints) {
        const cx = sx(pt.t);
        const cy = sy(pt.seq);
        const ratio = metricRange > 0 ? Math.min(1, Math.max(0, (pt.ms - minV) / metricRange)) : 0;
        const dot = svgEl("circle", {
          cx: cx.toFixed(2),
          cy: cy.toFixed(2),
          r: dotRadius.toFixed(2),
          fill: heatmapColor(ratio),
          "fill-opacity": "0.8",
          style: "cursor:pointer",
        });
        dot.addEventListener("mouseenter", (evt) => activateHover(pt, cx, cy, evt));
        dot.addEventListener("mousemove", (evt) => activateHover(pt, cx, cy, evt));
        dotsGroup.appendChild(dot);
        renderedDots.push({ pt, cx, cy });
      }

      plotBg.addEventListener("mousemove", (evt) => {
        const coords = clientToSvgCoords(evt);
        if (!coords) return;
        let best = null;
        let bestDist = 18 * 18;
        for (const item of renderedDots) {
          const dx = item.cx - coords.mx;
          const dy = item.cy - coords.my;
          const d2 = dx * dx + dy * dy;
          if (d2 <= bestDist) {
            bestDist = d2;
            best = item;
          }
        }
        if (!best) {
          clearHover();
          return;
        }
        activateHover(best.pt, best.cx, best.cy, evt);
      });
      svg.addEventListener("mouseleave", clearHover);

      svg.appendChild(dotsGroup);
      svg.appendChild(hoverRing);

      svg.appendChild(svgEl("text", {
        x: (pad.l + iw / 2).toFixed(2),
        y: height - 6,
        fill: "var(--text-muted)",
        "font-size": 10.5,
        "text-anchor": "middle",
      }, [document.createTextNode("Elapsed time (s)")]));

      svg.appendChild(svgEl("text", {
        transform: `translate(13 ${(pad.t + ih / 2).toFixed(2)}) rotate(-90)`,
        fill: "var(--text-muted)",
        "font-size": 10.5,
        "text-anchor": "middle",
      }, [document.createTextNode(seqLabel)]));

      const rawTotal = heatmap.points_total;
      const pointsTotal = isValidNonNeg(rawTotal) ? Math.max(validPoints.length, Math.round(Number(rawTotal))) : validPoints.length;
      const metaText = pointsTotal > validPoints.length
        ? `Showing ${intFmt(validPoints.length)} of ${intFmt(pointsTotal)} requests (uniformly sampled)`
        : `${intFmt(pointsTotal)} ${pointsTotal === 1 ? "request" : "requests"}`;
      const colorLegendPrefix = `Color: ${metricLabel} (${unitLabel})`;
      const scaleNodes = buildScaleNodes(colorLegendPrefix, minV, displayMaxV, metricRange);
      const legendStrip = el(
        "div",
        { style: "display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-top:6px;font-size:11px;color:var(--text-muted);font-family:var(--mono);" },
        [
          el("span", { textContent: metaText }),
          el("span", { style: "display:inline-flex;align-items:center;flex-wrap:wrap;gap:6px;", "aria-label": `${colorLegendPrefix} scale` }, scaleNodes),
        ]
      );

      container.replaceChildren(svg, legendStrip);
      return;
    }

    const minT = Math.min(0, ...cells.map((c) => c.start_seconds));
    const maxT = Math.max(minT + 0.001, ...cells.map((c) => c.end_seconds));
    const minS = Math.min(...cells.map((c) => c.seq_start));
    const maxS = Math.max(minS + 1, ...cells.map((c) => c.seq_end));
    const cellAvgs = cells.map((c) => c.avg_ms);
    const rawMinV = heatmap.min_ms ?? heatmap.minimum_metric_ms;
    const rawMaxV = heatmap.max_ms ?? heatmap.maximum_metric_ms;
    const minV = isFiniteMetric(rawMinV) ? Number(rawMinV) : Math.min(...cellAvgs);
    const displayMaxV = isFiniteMetric(rawMaxV) ? Number(rawMaxV) : Math.max(...cellAvgs);
    const metricRange = displayMaxV - minV;

    const sx = (t) => pad.l + ((t - minT) / (maxT - minT)) * iw;
    const sy = (s) => pad.t + ih - ((s - minS) / (maxS - minS)) * ih;

    const svg = svgEl("svg", {
      viewBox: `0 0 ${width} ${height}`,
      width: "100%",
      height,
      style: "display:block",
      role: "img",
      "aria-label": `${title} legacy binned heatmap by elapsed time and ${seqLabel.toLowerCase()}`,
    });

    svg.appendChild(svgEl("rect", {
      x: pad.l,
      y: pad.t,
      width: iw,
      height: ih,
      rx: 3,
      fill: "var(--bg)",
      stroke: "var(--border)",
      "stroke-width": 1,
    }));

    for (let i = 0; i <= 4; i++) {
      const tVal = minT + ((maxT - minT) * i) / 4;
      const gx = sx(tVal);
      if (i > 0 && i < 4) {
        svg.appendChild(svgEl("line", {
          x1: gx.toFixed(2),
          y1: pad.t,
          x2: gx.toFixed(2),
          y2: pad.t + ih,
          stroke: "var(--border)",
          "stroke-dasharray": "3 3",
          "stroke-width": 0.8,
        }));
      }
      const tLabel = `${fmtDec(tVal)}s`;
      svg.appendChild(svgEl("text", {
        x: gx.toFixed(2),
        y: pad.t + ih + 15,
        fill: "var(--text-muted)",
        "font-size": 10,
        "text-anchor": i === 0 ? "start" : (i === 4 ? "end" : "middle"),
      }, [document.createTextNode(tLabel)]));
    }

    for (let i = 0; i <= 4; i++) {
      const sVal = minS + ((maxS - minS) * i) / 4;
      const gy = sy(sVal);
      if (i > 0 && i < 4) {
        svg.appendChild(svgEl("line", {
          x1: pad.l,
          y1: gy.toFixed(2),
          x2: pad.l + iw,
          y2: gy.toFixed(2),
          stroke: "var(--border)",
          "stroke-dasharray": "3 3",
          "stroke-width": 0.8,
        }));
      }
      svg.appendChild(svgEl("text", {
        x: pad.l - 7,
        y: (gy + 3.5).toFixed(2),
        fill: "var(--text-muted)",
        "font-size": 10,
        "text-anchor": "end",
      }, [document.createTextNode(intFmt(sVal))]));
    }

    const cellsGroup = svgEl("g");
    for (const c of cells) {
      const x1 = sx(c.start_seconds);
      const x2 = sx(c.end_seconds);
      const yTop = sy(c.seq_end);
      const yBot = sy(c.seq_start);
      const cellW = Math.max(2, x2 - x1);
      const cellH = Math.max(2, yBot - yTop);
      const ratio = metricRange > 0 ? Math.min(1, Math.max(0, (c.avg_ms - minV) / metricRange)) : 0;
      const rect = svgEl("rect", {
        x: x1.toFixed(2),
        y: yTop.toFixed(2),
        width: cellW.toFixed(2),
        height: cellH.toFixed(2),
        rx: "1.5",
        ry: "1.5",
        fill: heatmapColor(ratio),
        stroke: "var(--bg-card)",
        "stroke-width": "0.75",
        style: "cursor:pointer",
      });
      const tipLines = [
        `Time: ${fmtDec(c.start_seconds)}s – ${fmtDec(c.end_seconds)}s`,
        `Tokens: ${intFmt(c.seq_start)} – ${intFmt(c.seq_end)} tok`,
        `Avg ${title}: ${valFmt(c.avg_ms)}`,
      ];
      if (isFiniteMetric(c.p50_ms)) tipLines.push(`P50 ${title}: ${valFmt(c.p50_ms)}`);
      if (isFiniteMetric(c.p90_ms)) tipLines.push(`P90 ${title}: ${valFmt(c.p90_ms)}`);
      if (isFiniteMetric(c.max_ms)) tipLines.push(`Max ${title}: ${valFmt(c.max_ms)}`);
      tipLines.push(`Requests: ${intFmt(c.count)}`);
      const tipText = tipLines.join("\n");

      rect.addEventListener("mouseenter", (evt) => {
        rect.setAttribute("stroke", "var(--text)");
        rect.setAttribute("stroke-width", "1.5");
        showTip(evt, tipText);
      });
      rect.addEventListener("mousemove", (evt) => showTip(evt, tipText));
      rect.addEventListener("mouseleave", () => {
        rect.setAttribute("stroke", "var(--bg-card)");
        rect.setAttribute("stroke-width", "0.75");
        hideTip();
      });
      cellsGroup.appendChild(rect);
    }
    svg.appendChild(cellsGroup);

    svg.appendChild(svgEl("text", {
      x: (pad.l + iw / 2).toFixed(2),
      y: height - 6,
      fill: "var(--text-muted)",
      "font-size": 10.5,
      "text-anchor": "middle",
    }, [document.createTextNode("Elapsed time")]));

    svg.appendChild(svgEl("text", {
      transform: `translate(13 ${(pad.t + ih / 2).toFixed(2)}) rotate(-90)`,
      fill: "var(--text-muted)",
      "font-size": 10.5,
      "text-anchor": "middle",
    }, [document.createTextNode(seqLabel)]));

    const timeBins = heatmap.time_bins ?? heatmap.time_bin_count;
    const lenBins = heatmap.len_bins ?? heatmap.sequence_length_bin_count;
    const totalBinned = cells.reduce((acc, c) => acc + Math.max(0, Number.isFinite(c.count) ? c.count : 0), 0);
    const hasGridResolution = isFiniteMetric(timeBins)
      && Number(timeBins) > 0
      && isFiniteMetric(lenBins)
      && Number(lenBins) > 0;
    const metaText = hasGridResolution
      ? `${intFmt(timeBins)} × ${intFmt(lenBins)} grid · ${intFmt(totalBinned)} requests (legacy binned fallback)`
      : `${intFmt(cells.length)} bins · ${intFmt(totalBinned)} requests (legacy binned fallback)`;

    const colorLegendPrefix = `Color: Avg ${metricLabel} (${unitLabel})`;
    const scaleNodes = buildScaleNodes(colorLegendPrefix, minV, displayMaxV, metricRange);

    const legendStrip = el(
      "div",
      { style: "display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-top:6px;font-size:11px;color:var(--text-muted);font-family:var(--mono);" },
      [
        el("span", { textContent: metaText }),
        el("span", { style: "display:inline-flex;align-items:center;flex-wrap:wrap;gap:6px;", "aria-label": `${colorLegendPrefix} scale` }, scaleNodes),
      ]
    );

    container.replaceChildren(svg, legendStrip);
    });
  }

  function CdfChart(container, cdfMap, sloMap) {
    cleanupChartMountsIn(container);
    const quantiles = Array.isArray(cdfMap && cdfMap.quantiles) ? cdfMap.quantiles : null;
    const tpotSloMs = sloMap && (sloMap.tpot_ms ?? (sloMap.tpot_s ? sloMap.tpot_s * 1000 : null));
    const defs = [
      { key: "ttft_ms", label: "TTFT (ms)", unit: "ms", color: PALETTE[0], slo: sloMap && sloMap.ttft_s ? sloMap.ttft_s * 1000 : null },
      { key: "tpot_ms", label: "TPOT (ms/tok)", unit: "ms/tok", color: PALETTE[1], slo: tpotSloMs },
      { key: "e2e_ms", label: "E2E (ms)", unit: "ms", color: PALETTE[2], slo: sloMap && sloMap.e2e_s ? sloMap.e2e_s * 1000 : null },
      { key: "drift_ms", label: "Schedule Drift (ms)", unit: "ms", color: PALETTE[3], slo: null },
    ];
    const entries = defs
      .map((d) => {
        const arr = cdfMap && cdfMap[d.key];
        if (!Array.isArray(arr) || !arr.length) return null;
        const pts = arr.map((item, i) => {
          if (typeof item === "number") return { percentile: quantiles && quantiles[i] !== undefined ? Number(quantiles[i]) : i, value: item };
          return { percentile: Number(item.percentile || 0), value: Number(item.value || 0) };
        });
        return { ...d, pts };
      })
      .filter(Boolean);

    if (!entries.length) {
      container.replaceChildren(el("div", { className: "empty-note", textContent: "CDF distribution points unavailable." }));
      return;
    }

    const wrap = el("div", { className: "grid-2" });
    entries.forEach((entry) => {
      const card = el("div", { className: "kpi" });
      card.appendChild(el("div", { className: "kpi-label" }, [el("span", { textContent: entry.label }), helpBadge(entry.label)]));
      const chartHost = el("div", { className: "chart-box" });
      card.appendChild(chartHost);
      wrap.appendChild(card);

      const height = 185;
      const pad = { l: 48, r: 14, t: 12, b: 26 };
      const ih = height - pad.t - pad.b;
      const pts = entry.pts;
      const maxX = Math.max(1e-3, ...pts.map((p) => Number(p.value || 0)), entry.slo || 0) * 1.05;
      const sy = (p) => pad.t + ih - (p / 100) * ih;

      mountResponsiveChart(chartHost, { minWidth: 280, fallbackWidth: 420 }, (width) => {
        const iw = width - pad.l - pad.r;
        const sx = (v) => pad.l + (Math.max(0, v) / maxX) * iw;
        const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height, style: "display:block" });
        const muted = cssVar("--text-muted");
        const grid = cssVar("--border");

        [0, 50, 90, 99, 100].forEach((p) => {
          const y = sy(p);
          svg.appendChild(svgEl("line", { x1: pad.l, y1: y, x2: pad.l + iw, y2: y, stroke: grid, "stroke-width": 0.7 }));
          svg.appendChild(svgEl("text", { x: pad.l - 5, y: y + 3, fill: muted, "font-size": 9.5, "text-anchor": "end" }, [document.createTextNode(p + "%")]));
        });
        if (entry.slo) {
          const xSlo = sx(entry.slo);
          svg.appendChild(svgEl("line", { x1: xSlo, y1: pad.t, x2: xSlo, y2: pad.t + ih, stroke: "#ef5350", "stroke-dasharray": "4 3", "stroke-width": 1.2 }));
        }
        if (pts.length > 1) {
          const d = pts.map((pt, i) => `${i === 0 ? "M" : "L"}${sx(pt.value).toFixed(1)},${sy(pt.percentile).toFixed(1)}`).join(" ");
          svg.appendChild(svgEl("path", { d, fill: "none", stroke: entry.color, "stroke-width": 2 }));
        } else if (pts.length === 1) {
          svg.appendChild(svgEl("circle", { cx: sx(pts[0].value).toFixed(1), cy: sy(pts[0].percentile).toFixed(1), r: 4, fill: entry.color }));
        }
        const overlay = svgEl("rect", { x: pad.l, y: pad.t, width: iw, height: ih, fill: "transparent" });
        overlay.addEventListener("mousemove", (evt) => {
          const rect = svg.getBoundingClientRect();
          const relY = ((evt.clientY - rect.top) / rect.height) * height;
          const targetPct = Math.min(100, Math.max(0, ((pad.t + ih - relY) / ih) * 100));
          let best = pts[0];
          for (const pt of pts) {
            if (Math.abs(pt.percentile - targetPct) < Math.abs(best.percentile - targetPct)) best = pt;
          }
          showTip(evt, `${entry.label}\nP${best.percentile}: ${num(best.value, 2)} ${entry.unit}`);
        });
        overlay.addEventListener("mouseleave", hideTip);
        svg.appendChild(overlay);
        chartHost.replaceChildren(svg);
      });
    });
    container.replaceChildren(wrap);
  }

  function makeSection(id, title, subtitle, bodyNode, rightNodeOrHelp, maybeHelpText, maybeFormula) {
    const isHelpStr = typeof rightNodeOrHelp === "string";
    const rightNode = isHelpStr ? null : rightNodeOrHelp;
    const explicitHelp = isHelpStr ? rightNodeOrHelp : maybeHelpText;
    const explicitFormula = isHelpStr ? maybeHelpText : maybeFormula;
    const resolvedHelp = resolveHelpText(id, explicitHelp) || resolveHelpText(title, null);
    const resolvedFormula = resolveHelpFormula(id, explicitFormula) || resolveHelpFormula(title, null);
    const subWrap = el("div", { className: "title-row" }, [
      subtitle ? el("span", { className: "section-sub", textContent: subtitle }) : null,
      rightNode || null,
    ]);
    return el("section", { className: "section", id }, [
      el("div", { className: "section-head" }, [
        el("h2", { className: "section-title" }, [
          el("span", { textContent: title }),
          helpBadge(title, resolvedHelp, resolvedFormula),
        ]),
        subtitle || rightNode ? subWrap : null,
      ]),
      bodyNode,
    ]);
  }

  function chartCard(title, chartNode, helpText, rightNode, helpFormula) {
    const resolved = resolveHelpText(title, helpText);
    const resolvedFormula = resolveHelpFormula(title, helpFormula);
    const head = el("div", { className: "chart-head" }, [
      el("span", { className: "chart-title" }, [
        el("span", { textContent: title }),
        helpBadge(title, resolved, resolvedFormula),
      ]),
      rightNode || null,
    ]);
    return el("div", null, [head, chartNode]);
  }

  function makeSortableTable(columns, rows, opts) {
    let sortCol = null;
    let sortAsc = true;
    const onRowClick = opts && typeof opts.onRowClick === "function" ? opts.onRowClick : null;
    const isRowSelected = opts && typeof opts.isRowSelected === "function" ? opts.isRowSelected : null;
    const wrap = el("div", { className: "table-wrap" });
    const table = el("table");
    const thead = el("thead");
    const tbody = el("tbody");

    function renderBody() {
      const sorted = rows.slice();
      if (sortCol !== null) {
        const col = columns[sortCol];
        sorted.sort((a, b) => {
          const av = col.getter ? col.getter(a) : a[col.key];
          const bv = col.getter ? col.getter(b) : b[col.key];
          if (av === bv) return 0;
          if (av === null || av === undefined) return 1;
          if (bv === null || bv === undefined) return -1;
          const cmp = typeof av === "number" && typeof bv === "number" ? av - bv : String(av).localeCompare(String(bv));
          return sortAsc ? cmp : -cmp;
        });
      }
      tbody.replaceChildren(
        ...sorted.map((row) => {
          const cls = [onRowClick ? "node-row" : "", isRowSelected && isRowSelected(row) ? "selected" : ""]
            .filter(Boolean)
            .join(" ");
          return el(
            "tr",
            {
              className: cls || null,
              onClick: onRowClick ? () => onRowClick(row) : null,
            },
            columns.map((col) => {
              const raw = col.getter ? col.getter(row) : row[col.key];
              if (col.render) {
                return el("td", { className: col.num ? "num" : "" }, [col.render(raw, row)]);
              }
              const text = col.format ? col.format(raw, row) : raw === null || raw === undefined ? "N/A" : String(raw);
              return el("td", { className: col.num ? "num" : "" }, [text]);
            })
          );
        })
      );
    }

    thead.appendChild(
      el(
        "tr",
        null,
        columns.map((col, idx) =>
          el(
            "th",
            {
              className: col.num ? "num" : "",
              onClick: () => {
                if (sortCol === idx) sortAsc = !sortAsc;
                else {
                  sortCol = idx;
                  sortAsc = !col.num;
                }
                renderBody();
              },
            },
            [
              el("span", { className: "th-inner" }, [
                el("span", { textContent: col.label }),
                helpBadge(col.label, col.helpText || col.help, col.helpFormula || col.formula),
              ]),
            ]
          )
        )
      )
    );
    renderBody();
    table.append(thead, tbody);
    wrap.appendChild(table);
    return wrap;
  }

  function getActiveWindowStats() {
    const cs = state.data.client_series || {};
    const windows = cs.windows || {};
    if (state.windowMode === "full") return windows.full || windows.profiling || {};
    return windows.profiling || windows.full || {};
  }

  function getHwLabel(st) {
    if (!st) return "Accelerator";
    const profile =
      st.hardware_profile ||
      (Array.isArray(st.hardware_profiles) && st.hardware_profiles.length ? st.hardware_profiles[0] : null);
    if (!profile) return "Accelerator";
    return (
      (profile.ui && profile.ui.labels && profile.ui.labels.metric_group) ||
      profile.metric_group ||
      profile.display_name ||
      "Accelerator"
    );
  }

  function renderOverview(pageNode) {
    const d = state.data;
    const f = d.forensics || {};
    const verdict = f.verdict || {};
    const cs = d.client_series || {};
    const st = d.server_telemetry || {};
    const win = getActiveWindowStats();
    const fullWin = (cs.windows && cs.windows.full) || {};
    const { warmupEnd } = resolveRunPhaseBoundaries(d);
    const sevClass = verdict.severity === "critical" ? "crit" : verdict.severity === "warning" ? "warn" : "good";

    const hero = el("div", { className: `hero ${sevClass}` }, [
      el("div", { className: "hero-top" }, [
        el("div", null, [
          el("div", { className: "kpi-label" }, [
            el("span", { textContent: `Automated Verdict · ${f.telemetry_scope || "client_only"}` }),
            helpBadge("Automated Verdict"),
          ]),
          el("div", { className: "hero-title" }, [
            el("span", { textContent: verdict.label || verdict.code || "Completed" }),
            " ",
            helpBadge("Dominant Limit"),
          ]),
        ]),
        el("div", { className: "title-row" }, [
          el("span", { className: `badge ${sevClass}` }, [
            el("span", { textContent: `SLO Attainment: ${pct(verdict.attainment_pct)}` }),
            helpBadge("SLO Attainment"),
          ]),
          el("span", { className: "badge info" }, [
            el("span", { textContent: `Goodput: ${num(verdict.goodput_rps)} req/s (${intFmt(verdict.goodput_per_hr)} / hr)` }),
            helpBadge("Goodput"),
          ]),
        ]),
      ]),
      el("div", { textContent: verdict.reason || "No bottleneck verdict summary recorded." }),
      Array.isArray(verdict.evidence) && verdict.evidence.length
        ? el(
            "ul",
            { className: "evidence-list" },
            verdict.evidence.map((item, idx) =>
              el("li", null, [
                el("button", {
                  type: "button",
                  className: "evidence-btn",
                  textContent: typeof item === "string" ? item : item.summary || item.message || JSON.stringify(item),
                  onClick: () => {
                    const target = (item && (item.tab || item.target_tab)) || (f.findings && f.findings[idx] && f.findings[idx].tab) || "bottlenecks";
                    if (target) {
                      switchPage(target);
                      const anchor = (item && item.anchor)
                        || (f.findings && f.findings[idx] && f.findings[idx].anchor)
                        || (typeof item === "string" && item.startsWith("Stage Breakdown") ? "b-stage-breakdown" : null);
                      if (anchor) {
                        const anchorEl = document.getElementById(anchor);
                        if (anchorEl && typeof anchorEl.scrollIntoView === "function") {
                          anchorEl.scrollIntoView({ behavior: "smooth" });
                        }
                      }
                    }
                  },
                }),
              ])
            )
          )
        : null,
    ]);
    pageNode.appendChild(makeSection("o-verdict", "Verdict & SLO Attainment", "Deterministic 8-rule forensics summary", hero));

    const hwLabel = getHwLabel(st);
    const summarySrv = st.summary || {};
    const energy = st.energy || {};
    const isDisaggOv = isDisaggregatedReport(st, d);
    const ovRoleLookup = buildRoleSeriesLookup(st.role_series);
    const ovCleanedPods = (Array.isArray(st.per_pod) ? st.per_pod : []).filter((p) => effectiveReportPodRole(p) !== "runner");
    const ovSrvPts = isDisaggOv ? serverSeriesPoints(st, cs) : [];
    const ovKv = resolveEffectiveKvMetrics(st, ovSrvPts, ovRoleLookup, ovCleanedPods, isDisaggOv);
    const ovStepSec = Number(st.step_seconds || 5);
    const ovDurationSec = isFiniteMetric(cs.duration_seconds)
      ? Number(cs.duration_seconds)
      : (isFiniteMetric(cs.windows && cs.windows.full && cs.windows.full.duration_seconds) ? Number(cs.windows.full.duration_seconds) : 0);
    const ovRoleSources = isDisaggOv
      ? deriveRoleTokenSourcesData(st, cs, ovSrvPts, st.role_series || [], summarySrv, st.derived || {}, ovCleanedPods, ovDurationSec, ovStepSec, true)
      : [];
    const ovHit = resolveEffectivePrefixHitMetrics(st, ovRoleSources, isDisaggOv);
    const kvPeakPct = ovKv.kvPeak;
    const hitPct = ovHit.hitAvg;
    const ttftDist = win.ttft_ms || win.ttft;
    const tpotDist = win.tpot_ms || win.tpot;
    const totalReq = win.request_count !== undefined ? win.request_count : win.total_requests;
    const fullReq = fullWin.request_count !== undefined ? fullWin.request_count : fullWin.total_requests;

    const kpiGrid = el("div", { className: "grid-kpis" }, [
      kpiTile("Completed / SLO-Compliant Requests", `${intFmt(totalReq)} / ${intFmt(win.good_requests)}`, `Full: ${intFmt(fullReq)} | Failed: ${intFmt(win.failed_requests)}`),
      kpiTile("Request Throughput / Goodput", `${num(win.throughput_rps)} / ${num(win.goodput_rps)} req/s`, `Output Throughput: ${num(win.output_tok_per_s, 1)} tok/s`),
      kpiTile("TTFT (P50 / P90 / P99)", `${msFmt(statVal(ttftDist, "p50_ms"))} / ${msFmt(statVal(ttftDist, "p90_ms"))}`, `P99: ${msFmt(statVal(ttftDist, "p99_ms"))}`),
      kpiTile("TPOT (P50 / P90 / P99)", `${tpotFmt(statVal(tpotDist, "p50_ms"))} / ${tpotFmt(statVal(tpotDist, "p90_ms"))}`, `P99: ${tpotFmt(statVal(tpotDist, "p99_ms"))}`),
      kpiTile("Error Rate", pct(win.error_rate_pct), `Failed: ${intFmt(win.failed_requests)}`),
      kpiTile("Peak KV Cache Usage", pct(kvPeakPct), `Prefix Cache Hit Rate: ${pct(hitPct)}`),
      kpiTile(`${hwLabel} Power (Avg / Peak)`, `${num(energy.accelerator_avg_power_watts, 1)} W / ${num(energy.accelerator_peak_power_watts, 1)} W`, `Energy: ${num(energy.accelerator_energy_wh, 2)} Wh (${num(energy.output_tokens_per_wh, 1)} tok/Wh)`),
    ]);
    pageNode.appendChild(makeSection("o-kpis", "Summary KPIs", `Window: ${state.windowMode}`, kpiGrid));

    const buckets = cs.buckets || cs.timeseries || [];
    const bucketX = (p) => Number(p.mid_seconds ?? p.time_seconds ?? p.start_seconds ?? p.t ?? 0);
    const { drainStart } = resolveRunPhaseBoundaries(d);
    const drainNote = drainStart !== null ? ` · Drain starts at ${fmtDec(drainStart)}s` : "";

    const thrHost = el("div", { className: "chart-box" });
    pageNode.appendChild(makeSection("o-thr", "Throughput over Time", `Completed vs Goodput (req/s) & Output Throughput (tok/s)${drainNote}`, thrHost));
    LineBandChart(thrHost, {
      warmupEnd,
      drainStart,
      rightLabel: "tok/s",
      series: [
        { label: "Completed req/s", points: buckets.map((p) => ({ x: bucketX(p), y: p.throughput_rps ?? p.completed_rps })) },
        { label: "Goodput req/s", points: buckets.map((p) => ({ x: bucketX(p), y: p.goodput_rps })) },
        { label: "Output Throughput (tok/s)", axis: "right", points: buckets.map((p) => ({ x: bucketX(p), y: p.output_tok_per_s })) },
      ],
    });

    const perfWrap = el("div");
    const perfControls = el("div", { className: "perf-controls" });
    const perfTabs = el("div", { className: "filter-bar", role: "group", "aria-label": "Latency metric" });
    const perfStatBar = el("div", { className: "filter-bar", role: "group", "aria-label": "Latency statistics" });
    const perfHost = el("div", { className: "chart-box" });
    const latencyModes = [
      { id: "all", label: "TTFT & TPOT" },
      { id: "ttft", label: "TTFT" },
      { id: "tpot", label: "TPOT" },
      { id: "e2e", label: "E2E" },
    ];
    const DEFAULT_LATENCY_STATS = ["p50", "mean", "p90"];
    const latencyStatDefs = [
      { key: "min", label: "Min", color: PALETTE[5], strokeDasharray: "2 3", strokeWidth: 1.5, opacity: 0.82 },
      { key: "p50", label: "P50", color: PALETTE[0], strokeDasharray: null, strokeWidth: 2.2, opacity: 1.0 },
      { key: "mean", label: "Mean", color: PALETTE[1], strokeDasharray: "6 3", strokeWidth: 1.8, opacity: 0.92 },
      { key: "p90", label: "P90", color: PALETTE[3], strokeDasharray: "4 2", strokeWidth: 1.8, opacity: 0.92 },
      { key: "p99", label: "P99", color: PALETTE[6], strokeDasharray: "8 3 2 3", strokeWidth: 1.8, opacity: 0.9 },
      { key: "max", label: "Max", color: PALETTE[4], strokeDasharray: "1.5 2.5", strokeWidth: 1.6, opacity: 0.85 },
    ];
    function bucketLatencyStat(bucket, metricKey, statKey) {
      if (!bucket || typeof bucket !== "object") return null;
      const raw = statVal(bucket[metricKey], statKey);
      if (isFiniteMetric(raw)) return Number(raw);
      const flatKey = `${metricKey}_${statKey}_ms`;
      if (isFiniteMetric(bucket[flatKey])) return Number(bucket[flatKey]);
      if (statKey === "mean" && isFiniteMetric(bucket[`${metricKey}_avg_ms`])) {
        return Number(bucket[`${metricKey}_avg_ms`]);
      }
      return null;
    }
    function activeMetricsForMode(mode) {
      if (mode === "ttft") return [{ key: "ttft", label: "TTFT", unit: "ms", formatValue: msFmt, baseColor: PALETTE[0] }];
      if (mode === "tpot") return [{ key: "tpot", label: "TPOT", unit: "ms/tok", formatValue: tpotFmt, baseColor: PALETTE[1] }];
      if (mode === "e2e") return [{ key: "e2e", label: "E2E", unit: "ms", formatValue: msFmt, baseColor: PALETTE[3] }];
      return [
        { key: "ttft", label: "TTFT", unit: "ms", formatValue: msFmt, baseColor: PALETTE[0] },
        { key: "tpot", label: "TPOT", unit: "ms/tok", formatValue: tpotFmt, baseColor: PALETTE[1] },
      ];
    }
    function isLatencyStatAvailable(statKey, metrics) {
      return buckets.some((p) => metrics.some((m) => bucketLatencyStat(p, m.key, statKey) !== null));
    }
    function setBtnDisabled(btn, isDisabled) {
      btn.disabled = Boolean(isDisabled);
      if (isDisabled) {
        btn.setAttribute("disabled", "disabled");
      } else {
        if (typeof btn.removeAttribute === "function") btn.removeAttribute("disabled");
        else if (btn.attributes && typeof btn.attributes === "object") delete btn.attributes.disabled;
      }
    }
    const modeButtons = [];
    const statButtons = [];
    let allDefaultBtn = null;

    function renderLatencyChart() {
      const slos = resolveConfiguredSlos(d);
      const mode = state.latencyMetric || "all";
      const metrics = activeMetricsForMode(mode);
      const availKeys = latencyStatDefs.filter((s) => isLatencyStatAvailable(s.key, metrics)).map((s) => s.key);
      let selected = Array.isArray(state.latencyStats) && state.latencyStats.length ? state.latencyStats.slice() : DEFAULT_LATENCY_STATS.slice();
      if (availKeys.length) {
        const activeAvail = selected.filter((k) => availKeys.includes(k));
        if (!activeAvail.length) {
          const defAvail = DEFAULT_LATENCY_STATS.filter((k) => availKeys.includes(k));
          selected = defAvail.length ? defAvail : [availKeys[0]];
          state.latencyStats = selected;
        }
      }
      const activeSet = new Set(selected);

      for (const item of modeButtons) {
        const isAct = mode === item.id;
        item.btn.className = `pill-btn${isAct ? " active" : ""}`;
        item.btn.setAttribute("aria-pressed", isAct ? "true" : "false");
      }
      for (const item of statButtons) {
        const avail = availKeys.includes(item.def.key);
        const isAct = avail && activeSet.has(item.def.key);
        item.btn.className = `pill-btn${isAct ? " active" : ""}${!avail ? " muted" : ""}`;
        item.btn.setAttribute("aria-pressed", isAct ? "true" : "false");
        setBtnDisabled(item.btn, !avail);
        item.btn.setAttribute(
          "title",
          avail ? `Toggle ${item.def.label} curve` : `${item.def.label} is unavailable for these legacy buckets`
        );
      }
      if (allDefaultBtn) {
        const hasExtraAvail = availKeys.some((k) => !DEFAULT_LATENCY_STATS.includes(k));
        const allAvailActive = availKeys.length > 0 && availKeys.every((k) => activeSet.has(k));
        const showDefault = allAvailActive && hasExtraAvail;
        const disableToggle = availKeys.length === 0 || (!hasExtraAvail && allAvailActive);
        allDefaultBtn.textContent = showDefault ? "Default" : "All";
        allDefaultBtn.className = `pill-btn${showDefault ? " active" : ""}${disableToggle ? " muted" : ""}`;
        allDefaultBtn.setAttribute("aria-pressed", showDefault ? "true" : "false");
        setBtnDisabled(allDefaultBtn, disableToggle);
        allDefaultBtn.setAttribute(
          "title",
          showDefault ? "Reset to default stats (P50, Mean, P90)" : "Select all available stats"
        );
      }

      const showBand = activeSet.has("p50") && activeSet.has("p90") && availKeys.includes("p50") && availKeys.includes("p90");
      const singleMetric = metrics.length === 1;
      const series = [];
      for (const m of metrics) {
        for (const statDef of latencyStatDefs) {
          if (!activeSet.has(statDef.key) || !isLatencyStatAvailable(statDef.key, [m])) continue;
          const isBandAnchor = showBand && statDef.key === "p50";
          const color = singleMetric ? statDef.color : m.baseColor;
          series.push({
            label: `${m.label} ${statDef.label} (${m.unit})`,
            color,
            strokeDasharray: statDef.strokeDasharray,
            strokeWidth: statDef.strokeWidth,
            opacity: statDef.opacity,
            formatValue: m.formatValue,
            hideBandTooltip: isBandAnchor,
            points: buckets.map((p) => {
              const y = bucketLatencyStat(p, m.key, statDef.key);
              const hi = isBandAnchor ? bucketLatencyStat(p, m.key, "p90") : null;
              return {
                x: bucketX(p),
                y,
                lo: isBandAnchor ? y : null,
                hi,
              };
            }),
          });
        }
      }

      const sloLine = mode === "tpot"
        ? slos.tpot_ms
        : mode === "e2e"
          ? slos.e2e_ms
          : slos.ttft_ms;
      const sloMetric = mode === "tpot" ? "TPOT" : mode === "e2e" ? "E2E" : "TTFT";
      const sloLabel = sloLine !== null ? `SLO: ${msFmt(sloLine)}` : null;
      const sloLegendLabel = sloLine !== null ? `${sloMetric} SLO (${msFmt(sloLine)})` : null;
      LineBandChart(perfHost, { warmupEnd, drainStart, sloLine, sloLabel, sloLegendLabel, series });
    }

    latencyModes.forEach((m) => {
      const btn = el("button", {
        type: "button",
        className: `pill-btn${state.latencyMetric === m.id ? " active" : ""}`,
        "aria-pressed": state.latencyMetric === m.id ? "true" : "false",
        textContent: m.label,
        onClick: () => {
          state.latencyMetric = m.id;
          renderLatencyChart();
        },
      });
      modeButtons.push({ id: m.id, btn });
      perfTabs.appendChild(btn);
    });

    latencyStatDefs.forEach((statDef) => {
      const isAct = (state.latencyStats || DEFAULT_LATENCY_STATS).includes(statDef.key);
      const btn = el("button", {
        type: "button",
        className: `pill-btn${isAct ? " active" : ""}`,
        "aria-pressed": isAct ? "true" : "false",
        "data-stat": statDef.key,
        textContent: statDef.label,
        onClick: () => {
          const mode = state.latencyMetric || "all";
          const metrics = activeMetricsForMode(mode);
          if (!isLatencyStatAvailable(statDef.key, metrics)) return;
          const availKeys = latencyStatDefs.filter((s) => isLatencyStatAvailable(s.key, metrics)).map((s) => s.key);
          const current = Array.isArray(state.latencyStats) ? state.latencyStats.slice() : DEFAULT_LATENCY_STATS.slice();
          const idx = current.indexOf(statDef.key);
          if (idx >= 0) {
            const remainingAvail = current.filter((k) => k !== statDef.key && availKeys.includes(k));
            if (!remainingAvail.length) return;
            current.splice(idx, 1);
          } else {
            current.push(statDef.key);
          }
          state.latencyStats = latencyStatDefs.map((s) => s.key).filter((k) => current.includes(k));
          renderLatencyChart();
        },
      });
      statButtons.push({ def: statDef, btn });
      perfStatBar.appendChild(btn);
    });

    allDefaultBtn = el("button", {
      type: "button",
      className: "pill-btn",
      "aria-pressed": "false",
      "data-stat-action": "toggle-all-default",
      textContent: "All",
      onClick: () => {
        const mode = state.latencyMetric || "all";
        const metrics = activeMetricsForMode(mode);
        const availKeys = latencyStatDefs.filter((s) => isLatencyStatAvailable(s.key, metrics)).map((s) => s.key);
        const current = Array.isArray(state.latencyStats) ? state.latencyStats : DEFAULT_LATENCY_STATS;
        const allAvailActive = availKeys.length > 0 && availKeys.every((k) => current.includes(k));
        if (allAvailActive) {
          const defAvail = DEFAULT_LATENCY_STATS.filter((k) => availKeys.includes(k));
          state.latencyStats = defAvail.length ? defAvail : (availKeys.length ? [availKeys[0]] : DEFAULT_LATENCY_STATS.slice());
        } else {
          state.latencyStats = availKeys.length ? availKeys : latencyStatDefs.map((s) => s.key);
        }
        renderLatencyChart();
      },
    });
    perfStatBar.appendChild(allDefaultBtn);

    perfControls.append(perfTabs, perfStatBar);
    perfWrap.append(perfControls, perfHost);
    pageNode.appendChild(makeSection("o-perf", "Latency over Time", `Selectable Min / P50 / Mean / P90 / P99 / Max (completed-request buckets, P50–P90 band)${drainNote}`, perfWrap));
    renderLatencyChart();

    const concHost = el("div", { className: "chart-box" });
    const srvPts = serverSeriesPoints(st, cs);
    const configuredConcurrency = resolveConfiguredConcurrency(d);
    pageNode.appendChild(makeSection("o-conc", "In-Flight Concurrency", `Client in-flight requests vs vLLM running requests${drainNote}`, concHost));
    LineBandChart(concHost, {
      warmupEnd,
      drainStart,
      refLine:
        configuredConcurrency !== null
          ? {
              value: configuredConcurrency,
              label: `Configured: ${intFmt(configuredConcurrency)}`,
              legendLabel: `Configured Concurrency (${intFmt(configuredConcurrency)})`,
              color: PALETTE[2],
            }
          : null,
      series: [
        { label: "Client In-Flight Requests", points: buckets.map((p) => ({ x: bucketX(p), y: p.in_flight_concurrency ?? p.in_flight })) },
        { label: "vLLM Running Requests", points: srvPts.map((p) => ({ x: p._t, y: p.running_requests ?? p.running })) },
      ],
    });

    const cold = cs.cold_start_comparison || {};
    const coldWarm = cold.warmup || cold.early_window || (cs.windows && cs.windows.warmup);
    const coldSteady = cold.steady_state || (cs.windows && cs.windows.profiling);
    const isAutoCold = cold.mode === "auto_10_percent" || cold.mode === "auto_10_pct";
    const coldLabel = isAutoCold ? "Warmup Phase (Auto 10%)" : "Warmup Phase";

    const COLD_STAT_DEFS = [
      { key: "min", label: "Min" },
      { key: "p50", label: "P50" },
      { key: "mean", label: "Mean" },
      { key: "p90", label: "P90" },
      { key: "p99", label: "P99" },
      { key: "max", label: "Max" },
    ];
    const COLD_METRIC_DEFS = [
      { key: "ttft", label: "TTFT", unit: "ms", formatValue: msFmt },
      { key: "tpot", label: "TPOT", unit: "ms/tok", formatValue: tpotFmt },
      { key: "e2e", label: "E2E", unit: "ms", formatValue: msFmt },
    ];
    function coldWindowStat(win, compactDist, metricKey, statKey) {
      const tryDist = (dist) => {
        if (!dist || typeof dist !== "object") return null;
        const val = statVal(dist, statKey);
        if (isFiniteMetric(val)) return Number(val);
        if (statKey === "mean" && isFiniteMetric(dist.avg_ms)) return Number(dist.avg_ms);
        return null;
      };
      if (win && typeof win === "object") {
        const nested = tryDist(win[`${metricKey}_ms`]) ?? tryDist(win[metricKey]);
        if (nested !== null) return nested;
        const flatKeys = [`${metricKey}_${statKey}_ms`, `${metricKey}_${statKey}`];
        if (statKey === "mean") flatKeys.push(`${metricKey}_avg_ms`, `${metricKey}_avg`, `${metricKey}Avg`, `${metricKey}Mean`);
        const capStat = statKey === "mean" ? "Mean" : statKey.charAt(0).toUpperCase() + statKey.slice(1);
        flatKeys.push(`${metricKey}${capStat}`);
        for (const fk of flatKeys) {
          if (isFiniteMetric(win[fk])) return Number(win[fk]);
        }
      }
      if (compactDist) {
        const compactVal = tryDist(compactDist);
        if (compactVal !== null) return compactVal;
      }
      return null;
    }
    function buildColdRowsForMetric(metricDef) {
      const warmCompact = cold[`warmup_${metricDef.key}_ms`];
      const steadyCompact = cold[`steady_${metricDef.key}_ms`];
      const rows = [];
      for (const stDef of COLD_STAT_DEFS) {
        const wVal = coldWindowStat(coldWarm, warmCompact, metricDef.key, stDef.key);
        const sVal = coldWindowStat(coldSteady, steadyCompact, metricDef.key, stDef.key);
        if (wVal !== null || sVal !== null) {
          rows.push({ statKey: stDef.key, statLabel: stDef.label, warm: wVal, steady: sVal });
        }
      }
      return rows;
    }

    const coldRowsByMetric = new Map();
    for (const mDef of COLD_METRIC_DEFS) {
      const rows = buildColdRowsForMetric(mDef);
      if (rows.length) coldRowsByMetric.set(mDef.key, rows);
    }
    const availableColdMetrics = COLD_METRIC_DEFS.filter((m) => coldRowsByMetric.has(m.key));

    let coldNode;
    if (availableColdMetrics.length) {
      const coldWrap = el("div");
      const coldFilterBar = el("div", { className: "filter-bar", role: "group", "aria-label": "Warmup comparison metric" });
      const coldChartHost = el("div", { className: "chart-box" });
      const coldSummaryHost = el("div", { className: "grid-2", style: "margin-top:10px" });
      const metricBtns = [];

      function renderColdComparison() {
        const activeMetric = availableColdMetrics.find((m) => m.key === state.coldLatencyMetric) || availableColdMetrics[0];
        state.coldLatencyMetric = activeMetric.key;
        for (const item of metricBtns) {
          const isAct = item.key === activeMetric.key;
          item.btn.className = `pill-btn${isAct ? " active" : ""}`;
          item.btn.setAttribute("aria-pressed", isAct ? "true" : "false");
        }
        const rows = coldRowsByMetric.get(activeMetric.key) || [];
        StackedBarAreaChart(coldChartHost, {
          grouped: true,
          yLabel: activeMetric.unit,
          formatValue: activeMetric.formatValue,
          emptyText: cold.note || "Warmup vs steady-state comparison unavailable.",
          items: rows.map((r) => ({
            label: r.statLabel,
            segments: [
              { label: "Warmup Phase", value: r.warm, color: PALETTE[2] },
              { label: "Steady-State", value: r.steady, color: PALETTE[0] },
            ],
          })),
        });

        const statMap = new Map(rows.map((r) => [r.statKey, r]));
        const warmP50 = statMap.get("p50") ? statMap.get("p50").warm : null;
        const steadyP50 = statMap.get("p50") ? statMap.get("p50").steady : null;
        const ratio = isFiniteMetric(warmP50) && isFiniteMetric(steadyP50) && Number(steadyP50) > 0
          ? Number(warmP50) / Number(steadyP50)
          : (activeMetric.key === "ttft" && isFiniteMetric(cold.cold_to_steady_ratio) ? Number(cold.cold_to_steady_ratio) : null);
        const ratioText = ratio !== null ? `${num(ratio)}x` : "N/A";
        const warmCount = coldWarm && (coldWarm.request_count ?? coldWarm.total_requests ?? coldWarm.count);
        const steadyCount = coldSteady && (coldSteady.request_count ?? coldSteady.total_requests ?? coldSteady.count);

        const formatStatSubline = (side) => {
          const parts = ["min", "mean", "p90", "p99", "max"].map((k) => {
            const stDef = COLD_STAT_DEFS.find((s) => s.key === k);
            const row = statMap.get(k);
            const val = row ? row[side] : null;
            return `${stDef.label}: ${activeMetric.formatValue(val)}`;
          });
          return `${activeMetric.label} ${parts.join(" · ")}`;
        };

        coldSummaryHost.replaceChildren(
          el("div", { className: "kpi" }, [
            el("div", { className: "kpi-label" }, [
              el("span", { textContent: coldLabel }),
              helpBadge(coldLabel),
            ]),
            el("div", { className: "kpi-val", textContent: activeMetric.formatValue(warmP50) }),
            el("div", { className: "kpi-sub", textContent: `n=${intFmt(warmCount)} | Warmup / Steady-State: ${ratioText}` }),
            el("div", { className: "kpi-sub", textContent: formatStatSubline("warm") }),
          ]),
          el("div", { className: "kpi" }, [
            el("div", { className: "kpi-label" }, [
              el("span", { textContent: `Steady-State Profiling Window (${ratioText})` }),
              helpBadge("Steady-State Profiling Window"),
            ]),
            el("div", { className: "kpi-val", textContent: activeMetric.formatValue(steadyP50) }),
            el("div", { className: "kpi-sub", textContent: `n=${intFmt(steadyCount)} | Warmup / Steady-State: ${ratioText}` }),
            el("div", { className: "kpi-sub", textContent: formatStatSubline("steady") }),
          ])
        );
      }

      if (availableColdMetrics.length >= 2) {
        availableColdMetrics.forEach((mDef) => {
          const isAct = mDef.key === (availableColdMetrics.some((m) => m.key === state.coldLatencyMetric) ? state.coldLatencyMetric : availableColdMetrics[0].key);
          const btn = el("button", {
            type: "button",
            className: `pill-btn${isAct ? " active" : ""}`,
            "aria-pressed": isAct ? "true" : "false",
            "data-cold-metric": mDef.key,
            textContent: mDef.label,
            onClick: () => {
              state.coldLatencyMetric = mDef.key;
              renderColdComparison();
            },
          });
          metricBtns.push({ key: mDef.key, btn });
          coldFilterBar.appendChild(btn);
        });
        coldWrap.appendChild(coldFilterBar);
      }
      coldWrap.appendChild(coldChartHost);
      coldWrap.appendChild(coldSummaryHost);
      renderColdComparison();
      coldNode = coldWrap;
    } else {
      coldNode = el("div", { className: "empty-note", textContent: cold.note || "Warmup vs steady-state comparison unavailable." });
    }
    const coldSubtitle = cold.note || (
      isAutoCold
        ? "Auto 10% warmup phase vs steady-state serving (Min, P50, Mean, P90, P99, Max)"
        : "Warmup phase latency vs steady-state profiling window (Min, P50, Mean, P90, P99, Max)"
    );
    pageNode.appendChild(makeSection("o-cold", "Warmup vs Steady-State Latency", coldSubtitle, coldNode));

    const pwrPods = (st.per_pod || []).filter((p) => p.gpu_power_watts || p.xpu_power_watts || p.power_watts);
    const pwrHost = el("div", { className: "chart-box" });
    pageNode.appendChild(makeSection("o-power", `${hwLabel} Power & Energy`, "Per-pod average vs peak power (W) and total energy", pwrHost));
    StackedBarAreaChart(pwrHost, {
      grouped: true,
      emptyText: `No ${hwLabel.toLowerCase()} power telemetry collected for this run.`,
      items: pwrPods.map((pod) => {
        const pwr = pod.gpu_power_watts || pod.xpu_power_watts || pod.power_watts || {};
        return {
          label: pod.pod || "pod",
          segments: [
            { label: "Avg Power (W)", value: pwr.mean || 0, color: PALETTE[0] },
            { label: "Peak Power (W)", value: pwr.max || 0, color: PALETTE[2] },
          ],
        };
      }),
    });
  }

  function renderSingleCdfSvg(host, pts, opts) {
    const cfg = opts || {};
    const cleanPts = (Array.isArray(pts) ? pts : [])
      .filter((p) => p && isFiniteMetric(p.percentile) && isFiniteMetric(p.value))
      .map((p) => ({ percentile: Number(p.percentile), value: Number(p.value) }))
      .sort((a, b) => a.percentile - b.percentile);
    if (!cleanPts.length) {
      cleanupChartContainer(host);
      host.replaceChildren(el("div", { className: "empty-note", textContent: cfg.emptyText || "CDF distribution points unavailable." }));
      return null;
    }

    const height = cfg.height || 210;
    const hasXLabel = Boolean(cfg.xLabel);
    const hasYLabel = Boolean(cfg.yLabel);
    const pad = { l: hasYLabel ? 60 : 48, r: 16, t: 16, b: hasXLabel ? 44 : 30 };
    const ih = height - pad.t - pad.b;

    const vals = cleanPts.map((p) => p.value);
    const rawMinX = Math.min(...vals);
    const rawMaxX = Math.max(...vals);
    const isDegenerate = rawMinX === rawMaxX;
    const minX = isDegenerate ? Math.max(0, rawMinX - 1) : rawMinX;
    const maxX = Math.max(minX + 1e-6, isDegenerate ? rawMaxX + 1 : rawMaxX);

    const sy = (p) => pad.t + ih - (Math.min(100, Math.max(0, p)) / 100) * ih;

    return mountResponsiveChart(host, { minWidth: 280, fallbackWidth: 480 }, (width) => {
      const iw = width - pad.l - pad.r;
      const sx = (v) => pad.l + ((v - minX) / (maxX - minX)) * iw;
      const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height, style: "display:block" });
    const grid = cssVar("--border");
    const muted = cssVar("--text-muted");
    const color = cfg.color || PALETTE[0];
    const unit = cfg.unit || "";

    [0, 25, 50, 75, 100].forEach((p) => {
      const y = sy(p);
      svg.appendChild(svgEl("line", { x1: pad.l, y1: y, x2: pad.l + iw, y2: y, stroke: grid, "stroke-width": 0.7 }));
      svg.appendChild(svgEl("text", { x: pad.l - 6, y: y + 3.5, fill: muted, "font-size": 10, "text-anchor": "end" }, [document.createTextNode(`${p}%`)]));
    });
    for (let i = 0; i <= 4; i++) {
      const xVal = minX + ((maxX - minX) * i) / 4;
      const x = sx(xVal);
      svg.appendChild(svgEl("text", { x, y: hasXLabel ? pad.t + ih + 15 : height - 8, fill: muted, "font-size": 10, "text-anchor": "middle" }, [document.createTextNode(fmtDec(xVal))]));
    }

    if (hasXLabel) {
      svg.appendChild(svgEl("text", {
        x: (pad.l + iw / 2).toFixed(2),
        y: height - 6,
        fill: muted,
        "font-size": 10.5,
        "text-anchor": "middle",
      }, [document.createTextNode(String(cfg.xLabel))]));
    }
    if (hasYLabel) {
      svg.appendChild(svgEl("text", {
        transform: `translate(14 ${(pad.t + ih / 2).toFixed(2)}) rotate(-90)`,
        fill: muted,
        "font-size": 10.5,
        "text-anchor": "middle",
      }, [document.createTextNode(String(cfg.yLabel))]));
    }

    if (cleanPts.length > 1) {
      const linePath = cleanPts.map((pt, i) => `${i === 0 ? "M" : "L"}${sx(pt.value).toFixed(1)},${sy(pt.percentile).toFixed(1)}`).join(" ");
      const firstX = sx(cleanPts[0].value).toFixed(1);
      const lastX = sx(cleanPts[cleanPts.length - 1].value).toFixed(1);
      const bottomY = sy(0).toFixed(1);
      const areaPath = `M${firstX},${bottomY} ${cleanPts.map((pt) => `L${sx(pt.value).toFixed(1)},${sy(pt.percentile).toFixed(1)}`).join(" ")} L${lastX},${bottomY} Z`;
      svg.appendChild(svgEl("path", { d: areaPath, fill: color, opacity: 0.14 }));
      svg.appendChild(svgEl("path", { d: linePath, fill: "none", stroke: color, "stroke-width": 2 }));
    } else {
      svg.appendChild(svgEl("circle", { cx: sx(cleanPts[0].value).toFixed(1), cy: sy(cleanPts[0].percentile).toFixed(1), r: 4, fill: color }));
    }

    const refDefs = [
      { label: "P50", val: cfg.p50, color: PALETTE[2] },
      { label: "P90", val: cfg.p90, color: PALETTE[4] },
    ].filter((r) => isFiniteMetric(r.val) && Number(r.val) >= minX && Number(r.val) <= maxX);
    let lastRefX = null;
    refDefs.forEach((ref, idx) => {
      const rx = sx(Number(ref.val));
      const yOffset = lastRefX !== null && Math.abs(rx - lastRefX) < 28 ? 11 * idx : 0;
      lastRefX = rx;
      svg.appendChild(svgEl("line", { x1: rx.toFixed(1), y1: pad.t, x2: rx.toFixed(1), y2: pad.t + ih, stroke: ref.color, "stroke-dasharray": "4 3", "stroke-width": 1.2 }));
      svg.appendChild(svgEl("text", { x: Math.min(pad.l + iw - 4, rx + 3).toFixed(1), y: pad.t + 10 + yOffset, fill: ref.color, "font-size": 9.5, "font-weight": 600 }, [document.createTextNode(ref.label)]));
    });

    const overlay = svgEl("rect", { x: pad.l, y: pad.t, width: iw, height: ih, fill: "transparent", style: "cursor:crosshair" });
    overlay.addEventListener("mousemove", (evt) => {
      const rect = svg.getBoundingClientRect();
      if (!rect || !rect.width || !rect.height) return;
      const mx = ((evt.clientX - rect.left) / rect.width) * width;
      const my = ((evt.clientY - rect.top) / rect.height) * height;
      let best = cleanPts[0];
      let bestDist = Infinity;
      for (const pt of cleanPts) {
        const dx = sx(pt.value) - mx;
        const dy = sy(pt.percentile) - my;
        const d2 = dx * dx + dy * dy;
        if (d2 < bestDist) {
          bestDist = d2;
          best = pt;
        }
      }
      const suffix = unit ? ` ${unit}` : "";
      showTip(evt, `${cfg.label || "CDF"}\nP${fmtDec(best.percentile)}: ${fmtDec(best.value)}${suffix}`);
    });
    overlay.addEventListener("mouseleave", hideTip);
    svg.appendChild(overlay);
    host.replaceChildren(svg);
    });
  }

  function IslOslScatterChart(host, opts) {
    const cfg = opts || {};
    const isValidNonneg = (v) =>
      v !== null && v !== undefined && typeof v !== "boolean" && isFiniteMetric(v) && Number(v) >= 0;
    const REGIMES = [
      { key: "prefill", label: "Prefill-Heavy (OSL / ISL < 0.25)", shortLabel: "Prefill-Heavy", color: PALETTE[0] },
      { key: "balanced", label: "Balanced (0.25 <= OSL / ISL <= 1.0)", shortLabel: "Balanced", color: PALETTE[1] },
      { key: "decode", label: "Decode-Heavy (OSL / ISL > 1.0)", shortLabel: "Decode-Heavy", color: PALETTE[2] },
    ];
    const rawPoints = Array.isArray(cfg.points) ? cfg.points : [];
    const points = [];
    for (const p of rawPoints) {
      if (!p || typeof p !== "object" || !isValidNonneg(p.isl) || !isValidNonneg(p.osl)) continue;
      const isl = Number(p.isl);
      const osl = Number(p.osl);
      const ratio = isl > 0 ? osl / isl : null;
      let regime;
      if (ratio === null) {
        regime = osl > 0 ? REGIMES[2] : REGIMES[1];
      } else if (ratio < 0.25) {
        regime = REGIMES[0];
      } else if (ratio <= 1.0) {
        regime = REGIMES[1];
      } else {
        regime = REGIMES[2];
      }
      points.push({
        isl,
        osl,
        ratio,
        regime,
        reqOsl: isValidNonneg(p.req_osl) ? Number(p.req_osl) : null,
        ttftMs: isValidNonneg(p.ttft_ms) ? Number(p.ttft_ms) : null,
        tpotMs: isValidNonneg(p.tpot_ms) ? Number(p.tpot_ms) : null,
        t: isValidNonneg(p.t) ? Number(p.t) : null,
        ok: p.ok !== false,
      });
    }

    if (!points.length) {
      cleanupChartContainer(host);
      host.replaceChildren(
        el("div", {
          className: "empty-note",
          textContent: cfg.emptyText || "Per-request Input vs Output Sequence Length points require raw request export.",
        })
      );
      return null;
    }

    const totalCount = isValidNonneg(cfg.totalCount) ? Math.max(points.length, Number(cfg.totalCount)) : points.length;
    const sampledCount = isValidNonneg(cfg.sampledCount) ? Number(cfg.sampledCount) : points.length;

    const height = cfg.height || 260;
    const pad = { l: 64, r: 20, t: 18, b: 44 };
    const ih = height - pad.t - pad.b;

    const maxX = Math.max(1, ...points.map((p) => p.isl));
    const maxY = Math.max(1, ...points.map((p) => p.osl));
    const sy = (v) => pad.t + ih - (v / maxY) * ih;
    const fmtTick = (val, maxVal) => (maxVal >= 4 ? intFmt(Math.round(val)) : fmtDec(val));

    return mountResponsiveChart(host, { minWidth: 300, fallbackWidth: 720 }, (width) => {
      const iw = width - pad.l - pad.r;
      const sx = (v) => pad.l + (v / maxX) * iw;
      const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height, style: "display:block" });
    const grid = cssVar("--border");
    const muted = cssVar("--text-muted");
    const crit = cssVar("--crit");

    for (let i = 0; i <= 4; i++) {
      const yVal = (maxY * i) / 4;
      const y = sy(yVal);
      svg.appendChild(svgEl("line", { x1: pad.l, y1: y, x2: pad.l + iw, y2: y, stroke: grid, "stroke-width": 0.7 }));
      svg.appendChild(
        svgEl("text", { x: pad.l - 6, y: y + 3.5, fill: muted, "font-size": 10, "text-anchor": "end" }, [
          document.createTextNode(fmtTick(yVal, maxY)),
        ])
      );
    }
    for (let i = 0; i <= 4; i++) {
      const xVal = (maxX * i) / 4;
      const x = sx(xVal);
      svg.appendChild(svgEl("line", { x1: x, y1: pad.t, x2: x, y2: pad.t + ih, stroke: grid, "stroke-width": 0.5, opacity: 0.45 }));
      svg.appendChild(
        svgEl("text", { x, y: pad.t + ih + 15, fill: muted, "font-size": 10, "text-anchor": "middle" }, [
          document.createTextNode(fmtTick(xVal, maxX)),
        ])
      );
    }

    svg.appendChild(
      svgEl(
        "text",
        {
          x: (pad.l + iw / 2).toFixed(2),
          y: height - 6,
          fill: muted,
          "font-size": 10.5,
          "text-anchor": "middle",
        },
        [document.createTextNode("Input Sequence Length (tokens)")]
      )
    );
    svg.appendChild(
      svgEl(
        "text",
        {
          transform: `translate(14 ${(pad.t + ih / 2).toFixed(2)}) rotate(-90)`,
          fill: muted,
          "font-size": 10.5,
          "text-anchor": "middle",
        },
        [document.createTextNode("Output Sequence Length (tokens)")]
      )
    );

    const diagEnd = Math.min(maxX, maxY);
    if (diagEnd > 0) {
      svg.appendChild(
        svgEl("line", {
          x1: sx(0).toFixed(1),
          y1: sy(0).toFixed(1),
          x2: sx(diagEnd).toFixed(1),
          y2: sy(diagEnd).toFixed(1),
          stroke: muted,
          "stroke-dasharray": "4 3",
          "stroke-width": 1.2,
          opacity: 0.85,
        })
      );
      const lblX = Math.max(pad.l + 72, Math.min(pad.l + iw - 4, sx(diagEnd) - 4));
      const lblY = Math.max(pad.t + 11, sy(diagEnd) - 6);
      svg.appendChild(
        svgEl(
          "text",
          {
            x: lblX.toFixed(1),
            y: lblY.toFixed(1),
            fill: muted,
            "font-size": 9.5,
            "font-weight": 600,
            "text-anchor": "end",
          },
          [document.createTextNode("OSL = ISL (1:1)")]
        )
      );
    }

    points.forEach((pt) => {
      const attrs = {
        cx: sx(pt.isl).toFixed(1),
        cy: sy(pt.osl).toFixed(1),
        r: 2.6,
        fill: pt.regime.color,
        "fill-opacity": 0.62,
      };
      if (!pt.ok) {
        attrs.stroke = crit;
        attrs["stroke-width"] = 0.9;
      }
      svg.appendChild(svgEl("circle", attrs));
    });

    const overlay = svgEl("rect", {
      x: pad.l,
      y: pad.t,
      width: iw,
      height: ih,
      fill: "transparent",
      style: "cursor:crosshair",
    });
    overlay.addEventListener("mousemove", (evt) => {
      const rect = svg.getBoundingClientRect();
      if (!rect || !rect.width || !rect.height) return;
      const mx = ((evt.clientX - rect.left) / rect.width) * width;
      const my = ((evt.clientY - rect.top) / rect.height) * height;
      let best = points[0];
      let bestDist = Infinity;
      for (const pt of points) {
        const dx = sx(pt.isl) - mx;
        const dy = sy(pt.osl) - my;
        const d2 = dx * dx + dy * dy;
        if (d2 < bestDist) {
          bestDist = d2;
          best = pt;
        }
      }
      const rows = [
        ["Input Sequence Length", `${intFmt(best.isl)} tok`],
        ["Output Sequence Length", `${intFmt(best.osl)} tok`],
        ["OSL / ISL Ratio", best.ratio !== null ? `${fmtDec(best.ratio)}×` : "N/A (ISL = 0)"],
        ["Regime", best.regime.shortLabel],
      ];
      if (best.reqOsl !== null) rows.push(["Requested OSL", `${intFmt(best.reqOsl)} tok`]);
      if (best.ttftMs !== null) rows.push(["TTFT", msFmt(best.ttftMs)]);
      if (best.tpotMs !== null) rows.push(["TPOT", tpotFmt(best.tpotMs)]);
      if (best.t !== null) rows.push(["Arrival Time", secFmt(best.t)]);
      rows.push(["Status", best.ok ? "Completed" : "Failed"]);

      tooltip.classList.remove("help-tip");
      tooltip.classList.add("stack-tip");
      tooltip.replaceChildren(
        el("div", { className: "stack-tip-head" }, [
          el("span", { className: "stack-tip-name" }, [
            el("span", { className: "stack-tip-dot", style: `background:${best.regime.color}` }),
            el("span", { textContent: best.regime.label }),
          ]),
        ]),
        ...rows.map(([label, val]) =>
          el("div", { className: "stack-tip-row" }, [
            el("span", { className: "stack-tip-name", textContent: label }),
            el("span", { className: "stack-tip-val", textContent: val }),
          ])
        )
      );
      tooltip.style.display = "block";
      placeTooltip(evt.clientX, evt.clientY);
    });
    overlay.addEventListener("mouseleave", hideTip);
    svg.appendChild(overlay);

    const outNodes = [svg, renderHbarLegend(REGIMES.map((r) => ({ label: r.label, color: r.color })))];
    if (sampledCount < totalCount) {
      outNodes.push(
        el("div", {
          className: "chart-subtitle",
          style: "margin-top:4px",
          textContent: `Showing ${intFmt(sampledCount)} of ${intFmt(totalCount)} requests`,
        })
      );
    }
    host.replaceChildren(...outNodes);
    });
  }

  function nonEmptyStr(...vals) {
    for (const v of vals) {
      if (typeof v === "string" && v.trim()) return v.trim();
    }
    return "";
  }

  function basenameOfPath(rawPath) {
    if (typeof rawPath !== "string" || !rawPath.trim()) return "";
    const parts = rawPath.split(/[\\/]/).filter(Boolean);
    return parts.length ? parts[parts.length - 1] : "";
  }

  function formatScenarioName(raw) {
    if (typeof raw !== "string" || !raw.trim()) return "";
    const norm = raw.trim().toLowerCase();
    if (norm === "chat") return "Chat";
    if (norm === "api-calling" || norm === "api_calling") return "Tool & API Use";
    if (norm === "coding") return "Code Generation";
    return raw
      .trim()
      .replace(/[_-]+/g, " ")
      .replace(/\b[a-z]/g, (ch) => ch.toUpperCase());
  }

  function resolveWorkloadPacing(data) {
    const d = data || {};
    const task = (d.task && typeof d.task === "object") ? d.task : {};
    const manifest = (d.run_manifest && typeof d.run_manifest === "object")
      ? d.run_manifest
      : ((d.manifest && typeof d.manifest === "object") ? d.manifest : {});
    const sim = (task.simulation && typeof task.simulation === "object") ? task.simulation : {};
    const trace = (task.prompt && task.prompt.trace && typeof task.prompt.trace === "object") ? task.prompt.trace : {};
    const manifestDataset = (manifest.dataset && typeof manifest.dataset === "object") ? manifest.dataset : {};
    const replay = (manifest.replay && typeof manifest.replay === "object") ? manifest.replay : {};

    const rawBackend = manifest.backend || sim.backend || task.backend || "";
    const backend = String(rawBackend).trim().toLowerCase();
    if (backend === "trace-replayer" || backend === "trace_replayer") {
      return {
        isFixedSchedule: true,
        concurrency: null,
        modeLabel: "Fixed Schedule",
        concurrencyLabel: "Fixed Schedule (trace-driven)",
      };
    }

    const traceFormat = nonEmptyStr(manifestDataset.format, trace.format, task.trace_format);
    const configuredConc = resolveConfiguredConcurrency(d);
    const rawFixedSchedule = replay.fixed_schedule !== undefined
      ? replay.fixed_schedule
      : (trace.fixed_schedule !== undefined ? trace.fixed_schedule : task.fixed_schedule);

    if (traceFormat === "weka_public_dataset" || rawFixedSchedule === false || configuredConc !== null) {
      const slots = configuredConc !== null ? configuredConc : 1;
      return {
        isFixedSchedule: false,
        concurrency: slots,
        modeLabel: "Concurrency",
        concurrencyLabel: `${intFmt(slots)} ${slots === 1 ? "slot" : "slots"}`,
      };
    }

    return {
      isFixedSchedule: true,
      concurrency: null,
      modeLabel: "Fixed Schedule",
      concurrencyLabel: "Fixed Schedule (trace-driven)",
    };
  }

  function firstNonNegFinite(...vals) {
    for (const v of vals) {
      if (typeof v !== "boolean" && isFiniteMetric(v) && Number(v) >= 0) return Number(v);
    }
    return null;
  }

  function firstPositiveFinite(...vals) {
    for (const v of vals) {
      const n = positiveFinite(v);
      if (n !== null) return n;
    }
    return null;
  }

  function buildWorkloadConfigSection(data) {
    const d = data || {};
    const task = (d.task && typeof d.task === "object") ? d.task : null;
    const manifest = (d.run_manifest && typeof d.run_manifest === "object")
      ? d.run_manifest
      : ((d.manifest && typeof d.manifest === "object") ? d.manifest : null);
    const hasContext = Boolean(
      (task && Object.keys(task).length > 0) ||
      (manifest && Object.keys(manifest).length > 0)
    );
    if (!hasContext) return null;

    const t = task || {};
    const m = manifest || {};
    const sim = (t.simulation && typeof t.simulation === "object") ? t.simulation : {};
    const prompt = (t.prompt && typeof t.prompt === "object") ? t.prompt : {};
    const trace = (prompt.trace && typeof prompt.trace === "object") ? prompt.trace : {};
    const promptDataset = (prompt.dataset && typeof prompt.dataset === "object") ? prompt.dataset : {};
    const manifestDataset = (m.dataset && typeof m.dataset === "object") ? m.dataset : {};
    const replay = (m.replay && typeof m.replay === "object") ? m.replay : {};
    const cs = (d.client_series && typeof d.client_series === "object") ? d.client_series : {};

    const datasetName = nonEmptyStr(
      manifestDataset.name,
      t.dataset,
      promptDataset.name,
      basenameOfPath(trace.path || t.trace_path || replay.trace_path)
    ) || "—";
    const traceFormat = nonEmptyStr(manifestDataset.format, trace.format, t.trace_format);
    const scenarioLabel = formatScenarioName(nonEmptyStr(manifestDataset.scenario, t.scenario, promptDataset.scenario));
    const datasetSubParts = [];
    if (traceFormat) datasetSubParts.push(`Format: ${traceFormat}`);
    if (scenarioLabel) datasetSubParts.push(`Scenario: ${scenarioLabel}`);
    const datasetSub = datasetSubParts.join(" · ") || "Trace dataset";

    const scaleFactor = firstPositiveFinite(
      trace.synthesis_speedup_ratio,
      t.scale_factor,
      replay.scale_factor,
      manifestDataset.scale_factor,
      m.scale_factor
    ) ?? 1;

    const replayStart = firstNonNegFinite(replay.start_seconds, t.start_seconds, trace.start_seconds) ?? 0;
    const rawEnd = firstPositiveFinite(replay.end_seconds, t.end_seconds, trace.end_seconds);
    const configuredEnd = (rawEnd !== null && rawEnd > replayStart) ? rawEnd : null;
    const durationSeconds = firstPositiveFinite(
      replay.duration_seconds,
      sim.duration_seconds,
      t.duration_seconds
    );
    const replayEnd = configuredEnd !== null
      ? configuredEnd
      : (durationSeconds !== null ? replayStart + durationSeconds * scaleFactor : null);

    const rangeVal = replayEnd !== null
      ? `${num(replayStart)}s – ${num(replayEnd)}s`
      : `From ${num(replayStart)}s (full trace)`;
    const rangeSub = replayEnd !== null
      ? (configuredEnd !== null
          ? `Source trace slice (${num(Math.max(0, replayEnd - replayStart))}s span)`
          : `Estimated slice (${num(Math.max(0, replayEnd - replayStart))}s at ${num(scaleFactor)}×)`)
      : "Unbounded source trace slice";

    const warmupSeconds = firstNonNegFinite(
      replay.warmup_seconds,
      m.warmup_seconds,
      t.warmup_seconds,
      t.backend_options && t.backend_options.warmup_seconds,
      replay.backend_options && replay.backend_options.warmup_seconds,
      sim.backend_options && sim.backend_options.warmup_seconds,
      cs.warmup_seconds
    ) ?? 0;
    const gracePeriodSeconds = firstNonNegFinite(
      replay.grace_period_seconds,
      sim.grace_period_seconds,
      t.grace_period_seconds
    );
    const durationVal = durationSeconds !== null ? secFmt(durationSeconds) : "N/A";
    const durationSubParts = [`Warmup: ${secFmt(warmupSeconds)}`];
    if (gracePeriodSeconds !== null && gracePeriodSeconds > 0) {
      durationSubParts.push(`Grace: ${secFmt(gracePeriodSeconds)}`);
    }
    const durationSub = durationSubParts.join(" · ");

    const pacing = resolveWorkloadPacing(d);
    const scaleSub = scaleFactor === 1
      ? "Real-time trace replay speed"
      : (scaleFactor > 1 ? "Accelerated inter-arrival replay" : "Slowed inter-arrival replay");

    const configGrid = el("div", { className: "grid-kpis" }, [
      kpiTile("Dataset", datasetName, datasetSub),
      kpiTile("Replay Time Range", rangeVal, rangeSub),
      kpiTile("Replay Duration & Warmup", durationVal, durationSub),
      kpiTile(
        "Pacing Mode",
        pacing.modeLabel,
        pacing.isFixedSchedule ? "Replays source trace timestamps" : "Maintains bounded in-flight slots"
      ),
      kpiTile(
        "Configured Concurrency",
        pacing.concurrencyLabel,
        pacing.isFixedSchedule ? "Arrival timing governed by trace" : "Max simultaneous in-flight request/session slots"
      ),
      kpiTile("Scale Factor", `${fmtDec(scaleFactor)}×`, scaleSub),
    ]);

    return makeSection(
      "w-config",
      "Workload & Replay Configuration",
      "Configured dataset, source trace slice, pacing mode, and timing parameters",
      configGrid
    );
  }

  function renderWorkload(pageNode) {
    const d = state.data || {};
    const configSection = buildWorkloadConfigSection(d);
    if (configSection) {
      pageNode.appendChild(configSection);
    }
    const cs = d.client_series || {};
    const wl = cs.workload && typeof cs.workload === "object" ? cs.workload : null;
    const wlWindows = wl && wl.windows && typeof wl.windows === "object" ? wl.windows : null;
    const winKey = state.windowMode === "full" ? "full" : "profiling";
    const wlWin = wlWindows ? (wlWindows[winKey] || wlWindows.profiling || wlWindows.full || null) : null;

    if (!wlWin) {
      pageNode.appendChild(
        makeSection(
          "w-kpis",
          "Summary",
          `Window: ${winKey === "full" ? "Full Run" : "Profiling Steady-State"}`,
          el("div", { className: "empty-note", textContent: "Workload distribution telemetry is unavailable for this run." })
        )
      );
      return;
    }

    const tokFmt = (v) => (isFiniteMetric(v) ? `${fmtDec(v)} tok` : "N/A");
    const ratioFmt = (v) => (isFiniteMetric(v) ? `${fmtDec(v)}×` : "N/A");
    const rpsFmt = (v) => (isFiniteMetric(v) ? `${fmtDec(v)} req/s` : "N/A");
    const cvFmt = (v) => (isFiniteMetric(v) ? fmtDec(v) : "N/A");

    const summary = wlWin.summary || {};
    const counts = summary.counts || {};
    const isl = summary.isl || {};
    const osl = summary.osl || {};
    const ratio = summary.osl_isl_ratio || {};
    const regimes = ratio.regimes || {};
    const conc = summary.concurrency || {};
    const arrivals = summary.arrivals || {};
    const { warmupEnd, drainStart } = resolveRunPhaseBoundaries(d);

    const kpiBody = el("div");
    const notices = [];
    if (wl.fallback_synthesized) {
      notices.push("Synthesized from aggregate run telemetry (raw per-request export unavailable). Per-request histograms and empirical CDFs require raw request export.");
    }
    if (wl.fallback_to_full && winKey === "profiling") {
      notices.push("Configured warmup equals or exceeds run duration; displaying full-run workload metrics for Profiling Steady-State.");
    }
    if (notices.length) {
      kpiBody.appendChild(el("div", { className: "stage-info-note", textContent: notices.join(" ") }));
    }

    const kpiGrid = el("div", { className: "grid-kpis" }, [
      kpiTile(
        "Requests & Tokens",
        intFmt(counts.total),
        `Completed: ${intFmt(counts.completed)} · Failed: ${intFmt(counts.failed)} · ISL: ${intFmt(summary.total_input_tokens)} · OSL: ${intFmt(summary.total_output_tokens)}`
      ),
      kpiTile(
        "Input Sequence Length (ISL P50)",
        tokFmt(isl.p50),
        `Mean: ${tokFmt(isl.mean)} · P90: ${tokFmt(isl.p90)} · P99: ${tokFmt(isl.p99)} · CV: ${cvFmt(isl.cv)}`
      ),
      kpiTile(
        "Output Sequence Length (OSL P50)",
        tokFmt(osl.p50),
        `Mean: ${tokFmt(osl.mean)} · P90: ${tokFmt(osl.p90)} · P99: ${tokFmt(osl.p99)} · CV: ${cvFmt(osl.cv)}`
      ),
      kpiTile(
        "OSL / ISL Ratio (P50)",
        ratioFmt(ratio.p50),
        `Mean: ${ratioFmt(ratio.mean)} · P90: ${ratioFmt(ratio.p90)}`
      ),
      kpiTile(
        "In-Flight Concurrency (Mean)",
        isFiniteMetric(conc.mean) ? `${fmtDec(conc.mean)} req` : "N/A",
        `P50: ${fmtDec(conc.p50)} · P90: ${fmtDec(conc.p90)} · P99: ${fmtDec(conc.p99)} · Peak: ${intFmt(conc.peak)}`
      ),
      kpiTile(
        "Mean Arrival Rate",
        rpsFmt(arrivals.mean_arrival_rps),
        `Peak: ${rpsFmt(arrivals.peak_arrival_rps)} · Burstiness: ${ratioFmt(arrivals.burstiness_peak_to_mean)} · Inter-Arrival CV: ${cvFmt(arrivals.inter_arrival_cv)}`
      ),
    ]);
    kpiBody.appendChild(kpiGrid);

    const prefillCnt = Number((regimes.prefill_heavy && regimes.prefill_heavy.count) || 0);
    const balancedCnt = Number((regimes.balanced && regimes.balanced.count) || 0);
    const decodeCnt = Number((regimes.decode_heavy && regimes.decode_heavy.count) || 0);
    const regimeTotal = prefillCnt + balancedCnt + decodeCnt;
    if (regimeTotal > 0) {
      const regimeSegs = [
        { label: `Prefill-Heavy (<0.25): ${intFmt(prefillCnt)} (${pct(regimes.prefill_heavy && regimes.prefill_heavy.share_pct)})`, value: prefillCnt, color: PALETTE[0] },
        { label: `Balanced (0.25–1.0): ${intFmt(balancedCnt)} (${pct(regimes.balanced && regimes.balanced.share_pct)})`, value: balancedCnt, color: PALETTE[1] },
        { label: `Decode-Heavy (>1.0): ${intFmt(decodeCnt)} (${pct(regimes.decode_heavy && regimes.decode_heavy.share_pct)})`, value: decodeCnt, color: PALETTE[2] },
      ];
      const regimePanel = el("div", { className: "hbar-panel", style: "margin-top:12px" }, [
        el("div", { className: "chart-head" }, [
          el("span", { className: "chart-title" }, [
            el("span", { textContent: "Workload Regime Mix (OSL / ISL Ratio)" }),
            helpBadge("Workload Regime Mix (OSL / ISL Ratio)"),
          ]),
          el("span", { className: "section-sub", textContent: `${intFmt(regimeTotal)} eligible requests` }),
        ]),
        HorizontalStackedBar({
          headerText: "Workload Regime Mix (OSL / ISL Ratio)",
          segments: regimeSegs,
          totalValue: regimeTotal,
          formatValue: intFmt,
          unitLabel: " req",
        }),
        renderHbarLegend(regimeSegs),
      ]);
      kpiBody.appendChild(regimePanel);
    }

    const winLabel = winKey === "full" ? "Full Run" : "Profiling Steady-State";
    pageNode.appendChild(
      makeSection(
        "w-kpis",
        "Summary",
        `Window: ${winLabel} · Duration: ${secFmt(wlWin.duration_seconds)}`,
        kpiBody
      )
    );

    function buildDistSection(opts) {
      const stats = opts.stats || {};
      const hist = Array.isArray(opts.hist) ? opts.hist : [];
      const cdfPts = Array.isArray(opts.cdf) ? opts.cdf : [];
      const body = el("div");
      body.appendChild(
        renderStatStrip([
          { label: "Min", value: tokFmt(stats.min) },
          { label: "P50", value: tokFmt(stats.p50) },
          { label: "Mean", value: tokFmt(stats.mean) },
          { label: "P90", value: tokFmt(stats.p90) },
          { label: "P99", value: tokFmt(stats.p99) },
          { label: "Max", value: tokFmt(stats.max) },
        ])
      );

      const grid = el("div", { className: "grid-2", style: "margin-top:10px" });
      const histHost = el("div", { className: "chart-box" });
      const cdfHost = el("div", { className: "chart-box" });

      const toggleBar = el("div", { className: "filter-bar", role: "group", "aria-label": `${opts.shortName} histogram unit`, style: "margin-bottom:0" });
      const modeBtns = [];
      [
        { id: "count", label: "Count" },
        { id: "share_pct", label: "Share %" },
      ].forEach((m) => {
        const btn = el("button", {
          type: "button",
          className: `pill-btn${state[opts.stateKey] === m.id ? " active" : ""}`,
          "aria-pressed": state[opts.stateKey] === m.id ? "true" : "false",
          "data-hist-mode": m.id,
          textContent: m.label,
          onClick: () => {
            state[opts.stateKey] = m.id;
            renderHist();
          },
        });
        modeBtns.push({ id: m.id, btn });
        toggleBar.appendChild(btn);
      });

      function renderHist() {
        const mode = state[opts.stateKey] === "share_pct" ? "share_pct" : "count";
        modeBtns.forEach((b) => {
          const act = b.id === mode;
          b.btn.className = `pill-btn${act ? " active" : ""}`;
          b.btn.setAttribute("aria-pressed", act ? "true" : "false");
        });
        StackedBarAreaChart(histHost, {
          grouped: true,
          xLabel: opts.xLabel,
          yLabel: mode === "share_pct" ? "% of Requests" : "Requests",
          formatValue: mode === "share_pct" ? pct : intFmt,
          emptyText: `${opts.shortName} histogram requires raw per-request export.`,
          items: hist.map((b) => ({
            label: String(b.label ?? `${intFmt(b.bin_start)}–${intFmt(b.bin_end)}`),
            segments: [
              {
                label: mode === "share_pct" ? "Share %" : "Requests",
                value: mode === "share_pct" ? (b.share_pct ?? 0) : (b.count ?? 0),
                color: opts.color,
                extra: mode === "share_pct" ? `${intFmt(b.count)} req` : pct(b.share_pct),
              },
            ],
          })),
        });
      }

      renderHist();
      renderSingleCdfSvg(cdfHost, cdfPts, {
        label: opts.cdfTitle,
        unit: "tok",
        color: opts.color,
        xLabel: opts.xLabel,
        yLabel: "% of Requests",
        p50: stats.p50,
        p90: stats.p90,
        emptyText: `${opts.shortName} CDF requires raw per-request export.`,
      });

      grid.append(
        chartCard(opts.histTitle, histHost, null, toggleBar),
        chartCard(opts.cdfTitle, cdfHost)
      );
      body.appendChild(grid);
      pageNode.appendChild(makeSection(opts.sectionId, opts.sectionTitle, opts.sectionSubtitle, body));
    }

    buildDistSection({
      sectionId: "w-isl",
      sectionTitle: "Input Sequence Length",
      sectionSubtitle: "Input token histogram, empirical CDF, and 6-stat summary",
      shortName: "Request ISL",
      stateKey: "workloadIslHistMode",
      stats: isl,
      hist: wlWin.isl_histogram,
      cdf: wlWin.isl_cdf,
      histTitle: "Request ISL Distribution",
      cdfTitle: "Request ISL CDF",
      xLabel: "Input Tokens",
      color: PALETTE[1],
    });

    buildDistSection({
      sectionId: "w-osl",
      sectionTitle: "Output Sequence Length",
      sectionSubtitle: "Generated token histogram, empirical CDF, and 6-stat summary",
      shortName: "Request OSL",
      stateKey: "workloadOslHistMode",
      stats: osl,
      hist: wlWin.osl_histogram,
      cdf: wlWin.osl_cdf,
      histTitle: "Request OSL Distribution",
      cdfTitle: "Request OSL CDF",
      xLabel: "Output Tokens",
      color: PALETTE[0],
    });

    const jointBuckets = Array.isArray(wlWin.isl_vs_osl_buckets) ? wlWin.isl_vs_osl_buckets : [];
    const hasJointOsl = jointBuckets.some((b) => isFiniteMetric(b.osl_p50) || isFiniteMetric(b.osl_mean) || isFiniteMetric(b.osl_p90));
    const hasJointRatio = jointBuckets.some((b) => isFiniteMetric(b.ratio_p50) || isFiniteMetric(b.ratio_mean));
    const requestPoints = Array.isArray(wlWin.request_points) ? wlWin.request_points : [];
    const jointBody = el("div");
    const scatterHost = el("div", { className: "chart-box tall" });
    jointBody.appendChild(chartCard("Input Sequence Length vs Output Sequence Length", scatterHost));

    if (jointBuckets.length && (!hasJointOsl || !hasJointRatio)) {
      jointBody.appendChild(
        el(
          "div",
          { style: "margin-top:12px" },
          [
            renderStatStrip(
              jointBuckets.map((b) => ({
                label: String(b.label || "Bucket"),
                value: `${intFmt(b.count)} req (${pct(b.share_pct)})`,
              }))
            ),
          ]
        )
      );
    }
    const jointGrid = el("div", { className: "grid-2", style: "margin-top:12px" });
    const jointOslHost = el("div", { className: "chart-box" });
    const jointRatioHost = el("div", { className: "chart-box" });
    jointGrid.append(
      chartCard("OSL by Input Sequence Length Bucket", jointOslHost),
      chartCard("Decode Intensity (OSL / ISL Ratio) by Input Sequence Length", jointRatioHost)
    );
    jointBody.appendChild(jointGrid);
    pageNode.appendChild(
      makeSection(
        "w-joint",
        "ISL vs OSL",
        "Per-request scatter, generated output sequence length, and OSL/ISL ratio by input sequence length",
        jointBody
      )
    );

    IslOslScatterChart(scatterHost, {
      points: requestPoints,
      totalCount: wlWin.request_points_total,
      sampledCount: wlWin.request_points_sampled,
      emptyText: "Per-request Input vs Output Sequence Length points require raw request export.",
    });

    StackedBarAreaChart(jointOslHost, {
      grouped: true,
      xLabel: "Input Sequence Length (ISL)",
      yLabel: "Output Tokens",
      formatValue: tokFmt,
      emptyText: "Per-bucket OSL statistics require raw per-request export.",
      items: hasJointOsl
        ? jointBuckets
            .filter((b) => isFiniteMetric(b.osl_p50) || isFiniteMetric(b.osl_mean) || isFiniteMetric(b.osl_p90))
            .map((b) => ({
              label: String(b.label || ""),
              segments: [
                { label: "OSL P50", value: b.osl_p50, color: PALETTE[1], extra: `n=${intFmt(b.count)}, ${pct(b.share_pct)}` },
                { label: "OSL Mean", value: b.osl_mean, color: PALETTE[0], extra: `n=${intFmt(b.count)}, ${pct(b.share_pct)}` },
                { label: "OSL P90", value: b.osl_p90, color: PALETTE[2], extra: `n=${intFmt(b.count)}, ${pct(b.share_pct)}` },
              ],
            }))
        : [],
    });

    StackedBarAreaChart(jointRatioHost, {
      grouped: true,
      xLabel: "Input Sequence Length (ISL)",
      yLabel: "OSL / ISL Ratio",
      formatValue: ratioFmt,
      emptyText: "Per-bucket OSL / ISL ratios require raw per-request export.",
      items: hasJointRatio
        ? jointBuckets
            .filter((b) => isFiniteMetric(b.ratio_p50) || isFiniteMetric(b.ratio_mean))
            .map((b) => ({
              label: String(b.label || ""),
              segments: [
                { label: "Ratio P50", value: b.ratio_p50, color: PALETTE[3], extra: `n=${intFmt(b.count)}, ${pct(b.share_pct)}` },
                { label: "Ratio Mean", value: b.ratio_mean, color: PALETTE[6], extra: `n=${intFmt(b.count)}, ${pct(b.share_pct)}` },
              ],
            }))
        : [],
    });

    const concBody = el("div");
    const residencyHost = el("div", { className: "chart-box" });
    concBody.appendChild(chartCard("In-Flight Concurrency Residency", residencyHost));

    const concGrid = el("div", { className: "grid-2", style: "margin-top:12px" });
    const concTimeHost = el("div", { className: "chart-box" });
    const rateTimeHost = el("div", { className: "chart-box" });
    concGrid.append(
      chartCard("In-Flight Concurrency over Time", concTimeHost),
      chartCard("Arrival vs Completion Rate over Time", rateTimeHost)
    );
    concBody.appendChild(concGrid);
    pageNode.appendChild(
      makeSection(
        "w-conc",
        "Concurrency & Arrival",
        "Exact interval residency distribution and time-series trajectories",
        concBody
      )
    );

    const residency = Array.isArray(wlWin.concurrency_residency) ? wlWin.concurrency_residency : [];
    StackedBarAreaChart(residencyHost, {
      grouped: true,
      xLabel: "In-Flight Concurrency (Requests)",
      yLabel: "% of Window",
      formatValue: pct,
      emptyText: "Concurrency residency requires per-request arrival and completion timestamps.",
      items: residency.map((b) => ({
        label: String(b.label ?? ""),
        segments: [
          {
            label: "Window Share",
            value: b.share_pct,
            color: PALETTE[1],
            extra: isFiniteMetric(b.duration_seconds) ? secFmt(b.duration_seconds) : undefined,
          },
        ],
      })),
    });

    const timeline = Array.isArray(wlWin.timeline) ? wlWin.timeline : [];
    const tlX = (p) => Number(p.t ?? p.mid_seconds ?? p.start_seconds ?? 0);
    LineBandChart(concTimeHost, {
      warmupEnd,
      drainStart,
      emptyText: "Concurrency timeline unavailable.",
      series: [
        {
          label: "In-Flight Avg",
          color: PALETTE[0],
          formatValue: (v) => `${fmtDec(v)} req`,
          points: timeline.map((p) => ({ x: tlX(p), y: p.in_flight_avg })),
        },
        {
          label: "In-Flight Max",
          color: PALETTE[2],
          dashed: true,
          formatValue: (v) => `${intFmt(v)} req`,
          points: timeline.map((p) => ({ x: tlX(p), y: p.in_flight_max })),
        },
      ],
    });

    LineBandChart(rateTimeHost, {
      warmupEnd,
      drainStart,
      emptyText: "Arrival and completion rate timeline unavailable.",
      series: [
        {
          label: "Arrival Rate",
          color: PALETTE[5],
          formatValue: rpsFmt,
          points: timeline.map((p) => ({ x: tlX(p), y: p.arrival_rps })),
        },
        {
          label: "Completion Rate",
          color: PALETTE[1],
          formatValue: rpsFmt,
          points: timeline.map((p) => ({ x: tlX(p), y: p.completed_rps })),
        },
      ],
    });

    const mt = wlWin.multi_turn || {};
    const mtBody = el("div");
    const hasPrefixShare = isFiniteMetric(mt.reusable_prefix_pct);
    const prefixSummaryVal = hasPrefixShare
      ? (isFiniteMetric(mt.reusable_prefix_tokens)
          ? `${pct(mt.reusable_prefix_pct)} (${intFmt(mt.reusable_prefix_tokens)} tok)`
          : pct(mt.reusable_prefix_pct))
      : "N/A";
    const mtStripItems = [
      isFiniteMetric(mt.session_count) && Number(mt.session_count) > 0
        ? { label: "Sessions", value: intFmt(mt.session_count) }
        : null,
      isFiniteMetric(mt.turns_per_session_mean)
        ? { label: "Mean Turns", value: fmtDec(mt.turns_per_session_mean) }
        : null,
      isFiniteMetric(mt.max_turns) && Number(mt.max_turns) > 0
        ? { label: "Max Turns", value: intFmt(mt.max_turns) }
        : null,
      hasPrefixShare ? { label: "Reusable Prefix Share", value: prefixSummaryVal } : null,
    ].filter(Boolean);

    if (mtStripItems.length) {
      mtBody.appendChild(renderStatStrip(mtStripItems));
    }

    if (mt.is_multi_turn) {
      const mtGrid = el("div", { className: "grid-2", style: mtStripItems.length ? "margin-top:10px" : null });
      const turnsDistHost = el("div", { className: "chart-box" });
      const islByTurnHost = el("div", { className: "chart-box" });
      mtGrid.append(
        chartCard("Turns per Session Distribution", turnsDistHost),
        chartCard("ISL Growth by Turn Depth", islByTurnHost)
      );
      mtBody.appendChild(mtGrid);

      const turnsDist = Array.isArray(mt.turns_distribution) ? mt.turns_distribution : [];
      StackedBarAreaChart(turnsDistHost, {
        grouped: true,
        xLabel: "Turns per Session",
        yLabel: "Sessions",
        formatValue: intFmt,
        emptyText: mt.reason || "Turns per session distribution unavailable.",
        items: turnsDist.map((b) => ({
          label: String(b.label || (b.turns === 1 ? "1 turn" : `${b.turns} turns`)),
          segments: [
            {
              label: "Sessions",
              value: b.sessions ?? b.count ?? 0,
              color: PALETTE[3],
              extra: isFiniteMetric(b.share_pct) ? pct(b.share_pct) : undefined,
            },
          ],
        })),
      });

      const islByTurn = Array.isArray(mt.isl_by_turn) ? mt.isl_by_turn : [];
      StackedBarAreaChart(islByTurnHost, {
        grouped: true,
        xLabel: "Turn Depth",
        yLabel: "Input Tokens",
        formatValue: tokFmt,
        emptyText: "Per-turn ISL growth requires per-request turn metadata.",
        items: islByTurn.map((b) => {
          const prefixExtra = isFiniteMetric(b.reusable_prefix_pct) ? `, prefix ${pct(b.reusable_prefix_pct)}` : "";
          const metaExtra = `n=${intFmt(b.request_count)}${prefixExtra}`;
          return {
            label: String(b.label || `Turn ${b.min_turn}`),
            segments: [
              { label: "ISL P50", value: b.isl_p50, color: PALETTE[0], extra: metaExtra },
              { label: "ISL Mean", value: b.isl_mean, color: PALETTE[1], extra: metaExtra },
              { label: "ISL P90", value: b.isl_p90, color: PALETTE[2], extra: metaExtra },
            ],
          };
        }),
      });
    } else {
      mtBody.appendChild(
        el("div", {
          className: "stage-info-note",
          style: mtStripItems.length ? "margin-top:10px" : null,
          textContent: mt.reason || "Single-turn or sessionless workload; multi-turn session depth charts are not applicable.",
        })
      );
    }

    pageNode.appendChild(
      makeSection(
        "w-multiturn",
        "Multi-Turn & Prefix",
        "Session turn counts, input sequence length growth across turns, and reusable leading-prefix share",
        mtBody
      )
    );
  }

  const OFFLINE_ROLE_LATENCY_KEYS = [
    "vllm_queue_time_p50_ms", "vllm_queue_time_p90_ms", "vllm_queue_time_p99_ms", "vllm_queue_time_mean_ms",
    "vllm_prefill_time_p50_ms", "vllm_prefill_time_p90_ms", "vllm_prefill_time_p99_ms", "vllm_prefill_time_mean_ms",
    "vllm_decode_time_p50_ms", "vllm_decode_time_p90_ms", "vllm_decode_time_p99_ms", "vllm_decode_time_mean_ms",
    "vllm_ttft_p50_ms", "vllm_ttft_p90_ms", "vllm_ttft_p99_ms", "vllm_ttft_mean_ms",
    "vllm_e2e_p50_ms", "vllm_e2e_p90_ms", "vllm_e2e_p99_ms", "vllm_e2e_mean_ms",
  ];
  const OFFLINE_PD_POOLED_CAVEAT = "Legacy P/D server telemetry pooled Prefill and Decode histograms together, distorting per-stage quantiles. Showing Client E2E/TTFT split with measured EPP Scheduling and NIXL KV Transfer; refresh telemetry against Prometheus or re-run for role-separated Queue/Prefill/Decode attribution.";

  function stageDisplayLabel(key, source) {
    if (key === "ttft_unattributed" && source === "pd_pooled_fallback") {
      return "P/D Engine TTFT (Queue + Prefill)";
    }
    const labels = {
      epp_scheduling: "EPP Scheduling",
      queue_time: "Queue Time",
      kv_transfer: "KV Transfer (NIXL)",
      prefill_time: "Prefill Time",
      engine_overhead: "Server TTFT Residual",
      gateway_overhead: "Client / Gateway / Network",
      decode_time: "Decode Time",
      ttft_unattributed: "Client TTFT (Unattributed)",
    };
    return labels[key] || String(key || "");
  }

  function nonnegFinite(v) {
    const n = firstFinite(v);
    return n !== null ? Math.max(0, n) : null;
  }

  function meanFinite(vals) {
    const clean = vals.filter((v) => v !== null && typeof v !== "boolean" && isFiniteMetric(v)).map(Number);
    return clean.length ? clean.reduce((a, b) => a + b, 0) / clean.length : null;
  }

  function routerStatVal(router, key, fallbackKey, pctKey) {
    const entry = (router && router[key]) ?? (fallbackKey && router && router[fallbackKey]);
    if (entry === null || entry === undefined || typeof entry === "boolean") return null;
    if (isFiniteMetric(entry)) return Math.max(0, Number(entry));
    if (typeof entry === "object") {
      return nonnegFinite(firstFinite(entry.mean, pctKey ? entry[pctKey] : null, entry.p50, entry.p90, entry.p95));
    }
    return null;
  }

  function hasRoleSeparatedLatency(st) {
    if (!st || typeof st !== "object") return false;
    const sbr = st.summary_by_role;
    const rs = Array.isArray(st.role_series) ? st.role_series : [];
    const hasPSummary = Boolean(sbr && sbr.prefill && OFFLINE_ROLE_LATENCY_KEYS.some((k) => sbr.prefill[k] !== undefined && sbr.prefill[k] !== null));
    const hasDSummary = Boolean(sbr && sbr.decode && OFFLINE_ROLE_LATENCY_KEYS.some((k) => sbr.decode[k] !== undefined && sbr.decode[k] !== null));
    const hasPSeries = rs.some((pt) => pt && typeof pt === "object" && OFFLINE_ROLE_LATENCY_KEYS.some((k) => isFiniteMetric(pt[`prefill_${k}`])));
    const hasDSeries = rs.some((pt) => pt && typeof pt === "object" && OFFLINE_ROLE_LATENCY_KEYS.some((k) => isFiniteMetric(pt[`decode_${k}`])));
    return (hasPSummary || hasPSeries) && (hasDSummary || hasDSeries);
  }

  function isLegacyPooledPd(f, st) {
    const slb = f && f.stage_latency_breakdown;
    const dec = f && f.ttft_decomposition;
    if ((slb && slb.source === "pd_pooled_fallback") || (dec && dec.source === "pd_pooled_fallback")) {
      return true;
    }
    if (slb && slb.source === "server_telemetry" && slb.role_breakdown && typeof slb.role_breakdown === "object") {
      const rb = slb.role_breakdown;
      const hasRb = ["prefill_p50_ms", "queue_p50_ms", "decode_p50_ms", "queue_raw_p50_ms", "prefill_mean_ms", "decode_mean_ms"]
        .some((k) => isFiniteMetric(rb.prefill && rb.prefill[k]) || isFiniteMetric(rb.decode && rb.decode[k]));
      if (hasRb) return false;
    }
    if (!st || st.available === false || st.status === "unavailable") return false;
    const ms = st.metric_sources || {};
    if (ms.vllm_stage_latencies === "pooled_namespace_legacy") return true;
    if (ms.vllm_stage_latencies === "role_separated" || ms.vllm_stage_latencies === "aggregated_namespace") return false;
    if (hasRoleSeparatedLatency(st)) return false;

    const topo = String(st.topology_mode || (st.topology && st.topology.mode) || "").toLowerCase();
    const rawPods = Array.isArray(st.per_pod) ? st.per_pod : [];
    const pods = rawPods.filter((p) => !isRunnerPod(p));
    const podRoles = new Set(pods.map((p) => effectiveReportPodRole(p)));
    const rs = Array.isArray(st.role_series) ? st.role_series : [];
    const sbr = st.summary_by_role || {};
    const rsum = (st.derived && st.derived.role_summary) || {};
    const comps = st.components || {};

    const hasP = podRoles.has("prefill")
      || Boolean(sbr.prefill)
      || Boolean(rsum.prefill)
      || (Array.isArray(comps.prefill) ? comps.prefill.length > 0 : Boolean(comps.prefill))
      || rs.some((pt) => pt && Object.keys(pt).some((k) => k.startsWith("prefill_")));
    const hasD = podRoles.has("decode")
      || Boolean(sbr.decode)
      || Boolean(rsum.decode)
      || (Array.isArray(comps.decode) ? comps.decode.length > 0 : Boolean(comps.decode))
      || rs.some((pt) => pt && Object.keys(pt).some((k) => k.startsWith("decode_")));
    const isDisagg = topo === "disaggregated" || (hasP && hasD);
    return Boolean(isDisagg && hasP && hasD);
  }

  function buildOfflineReconciliation(clientE2eMs, totalStageMs, quantile) {
    if (clientE2eMs === null || totalStageMs === null || clientE2eMs <= 0) return null;
    const gapMs = Math.round((clientE2eMs - totalStageMs) * 1000) / 1000;
    const gapPct = Math.round((gapMs / clientE2eMs) * 1000) / 10;
    return {
      client_e2e_ms: clientE2eMs,
      total_stage_ms: totalStageMs,
      e2e_gap_ms: gapMs,
      e2e_gap_pct: gapPct,
      quantile_non_additive: quantile !== "mean",
    };
  }

  function computeLegacyStageSlice(opts) {
    const { source, quantile, eppMs, queueMs, kvTransferMs, prefillMs, decodeMs, clientTtftMs, clientE2eMs, serverTtftMs, serverE2eMs } = opts;
    const round3 = (v) => Math.round(v * 1000) / 1000;
    if (source === "server_telemetry") {
      if (eppMs === null && queueMs === null && kvTransferMs === null && prefillMs === null && decodeMs === null) {
        return { available: false, source };
      }
      const knownEngineTtft = (queueMs || 0) + (kvTransferMs || 0) + (prefillMs || 0);
      const hasEngineTtft = queueMs !== null || kvTransferMs !== null || prefillMs !== null;
      const totalResidual = clientTtftMs !== null && hasEngineTtft
        ? round3(Math.max(0, clientTtftMs - ((eppMs || 0) + knownEngineTtft)))
        : null;
      const isMean = quantile === "mean";
      const serverTtftClamped = !isMean
        && serverTtftMs !== null
        && serverTtftMs >= 9500
        && clientTtftMs !== null
        && clientTtftMs > serverTtftMs + 500;
      let engineOvMs = null;
      let gatewayMs = null;
      let sliceCaveat = null;
      if (totalResidual !== null) {
        if (serverTtftClamped) {
          engineOvMs = totalResidual;
          gatewayMs = null;
          sliceCaveat = "Server TTFT quantile hit the 10 s histogram ceiling; unmeasured engine tail is attributed to Server TTFT Residual (see Mean slice for exact additive attribution).";
        } else if (serverTtftMs !== null) {
          engineOvMs = round3(Math.min(totalResidual, Math.max(0, serverTtftMs - knownEngineTtft)));
          gatewayMs = round3(Math.max(0, totalResidual - engineOvMs));
        } else {
          engineOvMs = null;
          gatewayMs = totalResidual;
        }
      }
      const ttftParts = [eppMs, queueMs, kvTransferMs, prefillMs, engineOvMs, gatewayMs].filter((v) => v !== null);
      const ttftStageMs = ttftParts.length ? round3(ttftParts.reduce((a, b) => a + b, 0)) : null;
      const totalParts = [eppMs, queueMs, kvTransferMs, prefillMs, engineOvMs, gatewayMs, decodeMs].filter((v) => v !== null);
      const totalStageMs = totalParts.length ? round3(totalParts.reduce((a, b) => a + b, 0)) : null;
      const shareE2e = {};
      for (const [k, v] of [
        ["epp_scheduling", eppMs],
        ["queue_time", queueMs],
        ["kv_transfer", kvTransferMs],
        ["prefill_time", prefillMs],
        ["engine_overhead", engineOvMs],
        ["gateway_overhead", gatewayMs],
        ["decode_time", decodeMs],
      ]) {
        shareE2e[k] = totalStageMs !== null && totalStageMs > 0 && v !== null ? Math.round((v / totalStageMs) * 1000) / 10 : null;
      }
      const shareTtft = {};
      for (const [k, v] of [
        ["epp_scheduling", eppMs],
        ["queue_time", queueMs],
        ["kv_transfer", kvTransferMs],
        ["prefill_time", prefillMs],
        ["engine_overhead", engineOvMs],
        ["gateway_overhead", gatewayMs],
      ]) {
        shareTtft[k] = ttftStageMs !== null && ttftStageMs > 0 && v !== null ? Math.round((v / ttftStageMs) * 1000) / 10 : null;
      }
      return {
        available: true,
        source,
        epp_scheduling_ms: eppMs,
        queue_time_ms: queueMs,
        kv_transfer_ms: kvTransferMs ?? null,
        prefill_time_ms: prefillMs,
        decode_time_ms: decodeMs,
        engine_overhead_ms: engineOvMs,
        gateway_overhead_ms: gatewayMs,
        ttft_stage_ms: ttftStageMs,
        total_stage_ms: totalStageMs,
        client_ttft_ms: clientTtftMs,
        client_e2e_ms: clientE2eMs,
        server_ttft_ms: serverTtftMs ?? null,
        server_e2e_ms: serverE2eMs ?? null,
        caveat: sliceCaveat,
        reconciliation: buildOfflineReconciliation(clientE2eMs, totalStageMs, quantile || "p50"),
        share_of_e2e_pct: shareE2e,
        share_of_ttft_pct: shareTtft,
      };
    }
    if (source === "pd_pooled_fallback") {
      if (clientTtftMs === null && clientE2eMs === null) {
        return { available: false, source: "pd_pooled_fallback" };
      }
      const kvMs = kvTransferMs !== null && kvTransferMs !== undefined
        ? (clientTtftMs !== null ? round3(Math.min(kvTransferMs, Math.max(0, clientTtftMs - (eppMs || 0)))) : round3(kvTransferMs))
        : null;
      const ttftUnattrMs = clientTtftMs !== null
        ? round3(Math.max(0, clientTtftMs - ((eppMs || 0) + (kvMs || 0))))
        : null;
      const decMs = clientE2eMs !== null && clientTtftMs !== null ? round3(Math.max(0, clientE2eMs - clientTtftMs)) : null;
      const ttftStageMs = clientTtftMs;
      const totalStageMs = ttftStageMs !== null || decMs !== null ? round3((ttftStageMs || 0) + (decMs || 0)) : null;
      return {
        available: true,
        source: "pd_pooled_fallback",
        epp_scheduling_ms: eppMs,
        queue_time_ms: null,
        kv_transfer_ms: kvMs,
        prefill_time_ms: null,
        decode_time_ms: decMs,
        engine_overhead_ms: null,
        gateway_overhead_ms: null,
        ttft_unattributed_ms: ttftUnattrMs,
        ttft_stage_ms: ttftStageMs,
        total_stage_ms: totalStageMs,
        client_ttft_ms: clientTtftMs,
        client_e2e_ms: clientE2eMs,
        reconciliation: buildOfflineReconciliation(clientE2eMs, totalStageMs, quantile || "p50"),
        share_of_e2e_pct: {
          epp_scheduling: totalStageMs > 0 && eppMs !== null ? Math.round((eppMs / totalStageMs) * 1000) / 10 : null,
          kv_transfer: totalStageMs > 0 && kvMs !== null ? Math.round((kvMs / totalStageMs) * 1000) / 10 : null,
          ttft_unattributed: totalStageMs > 0 && ttftUnattrMs !== null ? Math.round((ttftUnattrMs / totalStageMs) * 1000) / 10 : null,
          decode_time: totalStageMs > 0 && decMs !== null ? Math.round((decMs / totalStageMs) * 1000) / 10 : null,
        },
        share_of_ttft_pct: {
          epp_scheduling: ttftStageMs > 0 && eppMs !== null ? Math.round((eppMs / ttftStageMs) * 1000) / 10 : null,
          kv_transfer: ttftStageMs > 0 && kvMs !== null ? Math.round((kvMs / ttftStageMs) * 1000) / 10 : null,
          ttft_unattributed: ttftStageMs > 0 && ttftUnattrMs !== null ? Math.round((ttftUnattrMs / ttftStageMs) * 1000) / 10 : null,
        },
      };
    }
    if (clientTtftMs === null && clientE2eMs === null) {
      return { available: false, source: "client_fallback" };
    }
    const ttftStageMs = clientTtftMs;
    const decMs = clientE2eMs !== null && clientTtftMs !== null ? Math.max(0, clientE2eMs - clientTtftMs) : null;
    const totalStageMs = ttftStageMs !== null || decMs !== null ? (ttftStageMs || 0) + (decMs || 0) : null;
    return {
      available: true,
      source: "client_fallback",
      epp_scheduling_ms: null,
      queue_time_ms: null,
      kv_transfer_ms: null,
      prefill_time_ms: null,
      decode_time_ms: decMs,
      engine_overhead_ms: null,
      gateway_overhead_ms: null,
      ttft_stage_ms: ttftStageMs,
      total_stage_ms: totalStageMs,
      share_of_e2e_pct: {
        decode_time: totalStageMs !== null && totalStageMs > 0 && decMs !== null ? Math.round((decMs / totalStageMs) * 1000) / 10 : null,
        ttft_unattributed: totalStageMs !== null && totalStageMs > 0 && ttftStageMs !== null ? Math.round((ttftStageMs / totalStageMs) * 1000) / 10 : null,
      },
      share_of_ttft_pct: { epp_scheduling: null, queue_time: null, prefill_time: null, gateway_overhead: null },
    };
  }

  function dominantStageFromShare(shareObj, keys) {
    if (!shareObj || typeof shareObj !== "object") return null;
    let bestKey = null;
    let bestVal = 0;
    for (const k of keys) {
      const v = firstFinite(shareObj[k]);
      if (v !== null && v > bestVal) {
        bestVal = v;
        bestKey = k;
      }
    }
    return bestKey;
  }

  function findNearestOfflineSrvPt(pts, targetT, maxDist) {
    if (!Array.isArray(pts) || !pts.length || !isFiniteMetric(targetT)) return null;
    let best = null;
    let bestDist = Infinity;
    for (const p of pts) {
      if (!isFiniteMetric(p && p._t)) continue;
      const dist = Math.abs(Number(p._t) - Number(targetT));
      if (dist <= maxDist && dist < bestDist) {
        bestDist = dist;
        best = p;
      }
    }
    return best;
  }

  function offlineStageAreaLayers(source, hasKvTransfer) {
    if (source === "client_fallback") {
      return [
        { key: "ttft_ms", p90Key: "ttft_p90_ms", label: "Client TTFT (Unattributed)", color: PALETTE[0] },
        { key: "decode_ms", p90Key: "decode_p90_ms", label: "Decode Time (Est.)", color: PALETTE[1] },
      ];
    }
    if (source === "pd_pooled_fallback") {
      const pdLayers = [
        { key: "epp_sched_ms", p90Key: "epp_sched_p90_ms", label: "EPP Scheduling", color: PALETTE[3], dashed: true },
      ];
      if (hasKvTransfer) {
        pdLayers.push({ key: "kv_transfer_ms", p90Key: "kv_transfer_p90_ms", label: "KV Transfer (NIXL)", color: PALETTE[4] });
      }
      pdLayers.push(
        { key: "ttft_unattributed_ms", p90Key: "ttft_unattributed_p90_ms", fallbackKey: "ttft_ms", fallbackP90Key: "ttft_p90_ms", label: "P/D Engine TTFT (Queue + Prefill)", color: PALETTE[0] },
        { key: "decode_ms", p90Key: "decode_p90_ms", label: "Decode Time (Est.)", color: PALETTE[1] }
      );
      return pdLayers;
    }
    const layers = [
      { key: "epp_sched_ms", p90Key: "epp_sched_p90_ms", label: "EPP Scheduling", color: PALETTE[3], dashed: true },
      { key: "queue_ms", p90Key: "queue_p90_ms", label: "Queue Time", color: PALETTE[2] },
    ];
    if (hasKvTransfer) {
      layers.push({ key: "kv_transfer_ms", p90Key: "kv_transfer_p90_ms", label: "KV Transfer (NIXL)", color: PALETTE[4] });
    }
    layers.push(
      { key: "prefill_ms", p90Key: "prefill_p90_ms", label: "Prefill Time", color: PALETTE[0] },
      { key: "decode_ms", p90Key: "decode_p90_ms", label: "Decode Time", color: PALETTE[1] }
    );
    return layers;
  }

  function round3Offline(v) {
    return Math.round(v * 1000) / 1000;
  }

  function round1Offline(v) {
    return Math.round(v * 10) / 10;
  }

  function effectiveOfflineRoleQueue(qVal, qMeanVal, q) {
    const qNum = nonnegFinite(qVal);
    if (qNum === null) return null;
    const meanNum = nonnegFinite(qMeanVal);
    if (q !== "p50" && q !== "p90" && q !== "p99" && q !== "max") {
      return meanNum !== null ? meanNum : qNum;
    }
    const ceiling = q === "p50" ? 160 : (q === "p90" ? 280 : 305);
    if (meanNum !== null && meanNum < 25 && qNum <= ceiling) {
      return meanNum;
    }
    return qNum;
  }

  function reconcileOfflinePdEngineOverhead(opts) {
    const {
      engineOverheadMs,
      serverTtftClamped,
      pTtft,
      pQueueEff,
      pPrefill,
      dTtft,
      dQueueEff,
      dPrefill,
    } = opts;
    if (
      !isFiniteMetric(engineOverheadMs)
      || serverTtftClamped === true
      || !isFiniteMetric(pTtft)
      || !isFiniteMetric(pQueueEff)
      || !isFiniteMetric(pPrefill)
      || !isFiniteMetric(dTtft)
      || !isFiniteMetric(dQueueEff)
    ) {
      return {
        decode_first_step_ms: null,
        prefill_engine_overhead_ms: null,
        decode_handoff_overhead_ms: null,
      };
    }
    const target = round3Offline(Math.max(0, Number(engineOverheadMs)));
    if (target <= 0) {
      return {
        decode_first_step_ms: 0,
        prefill_engine_overhead_ms: 0,
        decode_handoff_overhead_ms: 0,
      };
    }
    const rawDPre = Math.max(0, isFiniteMetric(dPrefill) ? Number(dPrefill) : 0);
    const rawPOh = Math.max(0, Number(pTtft) - Number(pQueueEff) - Number(pPrefill));
    const rawDOh = Math.max(0, Number(dTtft) - Number(dQueueEff) - rawDPre);
    const raw = [rawDPre, rawPOh, rawDOh];
    const rawSum = raw[0] + raw[1] + raw[2];
    if (rawSum <= 0) {
      return {
        decode_first_step_ms: null,
        prefill_engine_overhead_ms: null,
        decode_handoff_overhead_ms: null,
      };
    }
    const scale = target / rawSum;
    const vals = raw.map((r) => round3Offline(r * scale));
    const drift = round3Offline(target - (vals[0] + vals[1] + vals[2]));
    if (drift !== 0) {
      let bestIdx = 0;
      for (let i = 1; i < raw.length; i += 1) {
        if (raw[i] > raw[bestIdx]) bestIdx = i;
      }
      vals[bestIdx] = round3Offline(Math.max(0, vals[bestIdx] + drift));
    }
    return {
      decode_first_step_ms: vals[0],
      prefill_engine_overhead_ms: vals[1],
      decode_handoff_overhead_ms: vals[2],
    };
  }

  function normalizeOfflineV6StageSlice(slbSlice, suffix) {
    if (
      !slbSlice
      || typeof slbSlice !== "object"
      || slbSlice.available !== true
      || slbSlice.source !== "server_telemetry"
      || Object.prototype.hasOwnProperty.call(slbSlice, "engine_overhead_ms")
      || !isFiniteMetric(slbSlice.server_ttft_ms)
      || !isFiniteMetric(slbSlice.gateway_overhead_ms)
    ) {
      return slbSlice || null;
    }
    const v6Residual = round3Offline(Math.max(0, Number(slbSlice.gateway_overhead_ms)));
    const serverTtftMs = Number(slbSlice.server_ttft_ms);
    const clientTtftMs = isFiniteMetric(slbSlice.client_ttft_ms) ? Number(slbSlice.client_ttft_ms) : null;
    const isClamped = Boolean(slbSlice.server_ttft_clamped)
      || (suffix !== "mean" && serverTtftMs >= 9500 && clientTtftMs !== null && clientTtftMs > serverTtftMs + 500);
    let engineOverheadMs;
    let gatewayOverheadMs;
    if (isClamped) {
      engineOverheadMs = v6Residual;
      gatewayOverheadMs = null;
    } else {
      const known = (nonnegFinite(slbSlice.queue_time_ms) || 0)
        + (nonnegFinite(slbSlice.kv_transfer_ms) || 0)
        + (nonnegFinite(slbSlice.prefill_time_ms) || 0);
      engineOverheadMs = round3Offline(Math.min(v6Residual, Math.max(0, serverTtftMs - known)));
      gatewayOverheadMs = round3Offline(Math.max(0, v6Residual - engineOverheadMs));
    }
    const e2eValues = {
      epp_scheduling: nonnegFinite(slbSlice.epp_scheduling_ms),
      queue_time: nonnegFinite(slbSlice.queue_time_ms),
      kv_transfer: nonnegFinite(slbSlice.kv_transfer_ms),
      prefill_time: nonnegFinite(slbSlice.prefill_time_ms),
      engine_overhead: engineOverheadMs,
      gateway_overhead: gatewayOverheadMs,
      decode_time: nonnegFinite(slbSlice.decode_time_ms),
    };
    const ttftValues = {
      epp_scheduling: e2eValues.epp_scheduling,
      queue_time: e2eValues.queue_time,
      kv_transfer: e2eValues.kv_transfer,
      prefill_time: e2eValues.prefill_time,
      engine_overhead: engineOverheadMs,
      gateway_overhead: gatewayOverheadMs,
    };
    const ttftParts = Object.values(ttftValues).filter((v) => v !== null);
    const ttftStageMs = isFiniteMetric(slbSlice.ttft_stage_ms)
      ? Number(slbSlice.ttft_stage_ms)
      : (ttftParts.length ? round3Offline(ttftParts.reduce((s, v) => s + v, 0)) : null);
    const totalParts = Object.values(e2eValues).filter((v) => v !== null);
    const totalStageMs = isFiniteMetric(slbSlice.total_stage_ms)
      ? Number(slbSlice.total_stage_ms)
      : (totalParts.length ? round3Offline(totalParts.reduce((s, v) => s + v, 0)) : null);
    const existingE2e = (slbSlice.share_of_e2e_pct && typeof slbSlice.share_of_e2e_pct === "object")
      ? slbSlice.share_of_e2e_pct
      : {};
    const existingTtft = (slbSlice.share_of_ttft_pct && typeof slbSlice.share_of_ttft_pct === "object")
      ? slbSlice.share_of_ttft_pct
      : {};
    const validE2eDenom = totalStageMs !== null && Number.isFinite(totalStageMs) && totalStageMs > 0;
    const validTtftDenom = ttftStageMs !== null && Number.isFinite(ttftStageMs) && ttftStageMs > 0;
    return {
      ...slbSlice,
      engine_overhead_ms: engineOverheadMs,
      gateway_overhead_ms: gatewayOverheadMs,
      ...(isClamped ? { server_ttft_clamped: true } : {}),
      share_of_e2e_pct: {
        ...existingE2e,
        engine_overhead: validE2eDenom && engineOverheadMs !== null ? round1Offline((engineOverheadMs / totalStageMs) * 100) : null,
        gateway_overhead: validE2eDenom && gatewayOverheadMs !== null ? round1Offline((gatewayOverheadMs / totalStageMs) * 100) : null,
      },
      share_of_ttft_pct: {
        ...existingTtft,
        engine_overhead: validTtftDenom && engineOverheadMs !== null ? round1Offline((engineOverheadMs / ttftStageMs) * 100) : null,
        gateway_overhead: validTtftDenom && gatewayOverheadMs !== null ? round1Offline((gatewayOverheadMs / ttftStageMs) * 100) : null,
      },
    };
  }

  function hasServerLatencyTelemetry(st) {
    if (!st || typeof st !== "object") return false;
    const avail = st.status !== "unavailable" && st.available !== false && (st.status === "available" || st.available === true);
    if (!avail) return false;
    const latPrefixes = [
      "vllm_prefill_time_",
      "vllm_queue_time_",
      "vllm_decode_time_",
      "vllm_ttft_",
      "nixl_transfer_latency_",
    ];
    const hasMetricVal = (v) => {
      if (v === null || v === undefined || typeof v === "boolean") return false;
      if (isFiniteMetric(v)) return true;
      if (typeof v === "object") {
        return ["mean", "p50", "p90", "p95", "p99", "max", "min"].some((k) => isFiniteMetric(v[k]));
      }
      return false;
    };
    const hasMapLat = (obj, extraPrefixes) => {
      if (!obj || typeof obj !== "object") return false;
      const all = extraPrefixes ? latPrefixes.concat(extraPrefixes) : latPrefixes;
      return Object.entries(obj).some(([k, v]) => all.some((p) => k.startsWith(p)) && hasMetricVal(v));
    };
    if (hasMapLat(st.summary)) return true;
    const sbr = st.summary_by_role || {};
    if (hasMapLat(sbr.prefill) || hasMapLat(sbr.decode)) return true;
    if (hasMapLat(st.router, ["scheduler_latency_", "scheduler_e2e_"])) return true;
    const series = Array.isArray(st.series) ? st.series : [];
    if (series.some((pt) => hasMapLat(pt, ["router_scheduler_latency_", "router_scheduler_e2e_", "prefill_time_", "queue_time_", "decode_time_"]))) {
      return true;
    }
    const roleSeries = Array.isArray(st.role_series) ? st.role_series : [];
    return roleSeries.some((pt) => pt && typeof pt === "object" && Object.entries(pt).some(([k, v]) => {
      if (!k.startsWith("prefill_") && !k.startsWith("decode_")) return false;
      const sub = k.slice(k.indexOf("_") + 1);
      return latPrefixes.some((p) => sub.startsWith(p)) && hasMetricVal(v);
    }));
  }

  function resolveSteadyClientWindow(cs) {
    const wins = (cs && cs.windows) || {};
    const warmupSec = firstFinite(cs && cs.warmup_seconds, state.data && state.data.task && state.data.task.warmup_seconds, 0) || 0;
    return wins.steady || (warmupSec <= 0 ? (wins.full || wins.profiling) : (wins.profiling || wins.full)) || {};
  }

  function hasClientSteadyWindow(cs) {
    const win = resolveSteadyClientWindow(cs);
    return Boolean(win && typeof win === "object" && (win.ttft_ms || win.ttft || win.e2e_ms || win.e2e));
  }

  function filterSteadyOfflinePts(rawPts, originEpoch, warmupSec, hasPrecomputedT) {
    const list = Array.isArray(rawPts) ? rawPts : [];
    if (!list.length || !(warmupSec > 0) || !(originEpoch > 1e9)) return list;
    const steady = list.filter((pt) => {
      if (!pt || typeof pt !== "object") return false;
      if (typeof pt.timestamp !== "string") return true;
      const t = hasPrecomputedT && isFiniteMetric(pt._t) ? Number(pt._t) : ((Date.parse(pt.timestamp) / 1000) - originEpoch);
      if (!Number.isFinite(t)) return true;
      return t >= warmupSec;
    });
    return steady.length ? steady : list;
  }

  function percentileOffline(vals, pctVal) {
    const clean = vals.filter((v) => v !== null && typeof v !== "boolean" && isFiniteMetric(v)).map(Number).sort((a, b) => a - b);
    if (!clean.length) return null;
    const idx = Math.min(clean.length - 1, Math.max(0, Math.ceil((clean.length * pctVal) / 100) - 1));
    return clean[idx];
  }

  function aggregateOfflineVals(vals, agg) {
    if (agg === "p99") return percentileOffline(vals, 99);
    if (agg === "max") {
      const clean = vals.filter((v) => v !== null && typeof v !== "boolean" && isFiniteMetric(v)).map(Number);
      return clean.length ? Math.max(...clean) : null;
    }
    return meanFinite(vals);
  }

  function summaryStatOffline(entry, agg) {
    if (entry === null || entry === undefined || typeof entry === "boolean") return null;
    if (isFiniteMetric(entry)) return Math.max(0, Number(entry));
    if (typeof entry === "object") {
      if (agg === "p99") return nonnegFinite(firstFinite(entry.p99, entry.max, entry.mean));
      if (agg === "max") return nonnegFinite(firstFinite(entry.max, entry.p99, entry.mean));
      return nonnegFinite(entry.mean);
    }
    return null;
  }

  function srvAggVal(steadySrvPts, summary, seriesKeys, summaryKey, warmupSec, agg) {
    const steadyVals = [];
    for (const pt of steadySrvPts) {
      if (!pt || typeof pt !== "object") continue;
      for (const sk of seriesKeys) {
        const v = nonnegFinite(pt[sk]);
        if (v !== null) {
          steadyVals.push(v);
          break;
        }
      }
    }
    if (warmupSec > 0 && steadyVals.length) {
      return round3Offline(aggregateOfflineVals(steadyVals, agg));
    }
    const sumVal = summaryStatOffline(summary && summary[summaryKey], agg);
    if (sumVal !== null) return round3Offline(sumVal);
    if (steadyVals.length) return round3Offline(aggregateOfflineVals(steadyVals, agg));
    return null;
  }

  function roleAggVal(steadyRolePts, summaryByRole, derivedRoleSummary, role, metricKey, warmupSec, agg) {
    const seriesKey = `${role}_${metricKey}`;
    const steadyVals = [];
    for (const pt of steadyRolePts) {
      if (!pt || typeof pt !== "object") continue;
      const v = nonnegFinite(pt[seriesKey]);
      if (v !== null) steadyVals.push(v);
    }
    if (warmupSec > 0 && steadyVals.length) {
      return round3Offline(aggregateOfflineVals(steadyVals, agg));
    }
    const roleSum = summaryByRole && typeof summaryByRole[role] === "object" ? summaryByRole[role] : {};
    const sumVal = summaryStatOffline(roleSum[metricKey], agg);
    if (sumVal !== null) return round3Offline(sumVal);
    const drv = derivedRoleSummary && typeof derivedRoleSummary[role] === "object" ? derivedRoleSummary[role] : {};
    const drvVal = nonnegFinite(drv[metricKey]);
    if (drvVal !== null) return round3Offline(drvVal);
    if (steadyVals.length) return round3Offline(aggregateOfflineVals(steadyVals, agg));
    return null;
  }

  function eppAggVal(steadySrvPts, router, seriesKeys, routerKeys, routerStatKeys, warmupSec, agg) {
    const steadyVals = [];
    for (const pt of steadySrvPts) {
      if (!pt || typeof pt !== "object") continue;
      for (const sk of seriesKeys) {
        const v = nonnegFinite(pt[sk]);
        if (v !== null) {
          steadyVals.push(v);
          break;
        }
      }
    }
    if (warmupSec > 0 && steadyVals.length) {
      return round3Offline(aggregateOfflineVals(steadyVals, agg));
    }
    for (const rk of routerKeys) {
      const entry = router && router[rk];
      if (entry === null || entry === undefined || typeof entry === "boolean") continue;
      if (isFiniteMetric(entry)) return round3Offline(Math.max(0, Number(entry)));
      if (typeof entry === "object") {
        for (const sk of routerStatKeys) {
          const val = nonnegFinite(entry[sk]);
          if (val !== null) return round3Offline(val);
        }
      }
    }
    if (steadyVals.length) return round3Offline(aggregateOfflineVals(steadyVals, agg));
    return null;
  }

  function normalizeNonAdditiveOverAlloc(parts, clientTtft) {
    if (!isFiniteMetric(clientTtft) || Number(clientTtft) <= 0) return parts;
    const keys = ["epp", "queue", "kv", "prefill"];
    const active = keys.filter((k) => isFiniteMetric(parts[k]));
    if (!active.length) return parts;
    const total = active.reduce((acc, k) => acc + Number(parts[k]), 0);
    const target = round3Offline(Number(clientTtft));
    if (total <= target) return parts;
    const scale = target / total;
    const next = { ...parts };
    for (const k of active) {
      next[k] = round3Offline(Math.max(0, Number(parts[k]) * scale));
    }
    const roundedSum = active.reduce((acc, k) => acc + Number(next[k]), 0);
    const drift = round3Offline(target - roundedSum);
    if (drift !== 0) {
      let bestKey = active[0];
      for (const k of active) {
        if (Number(parts[k]) > Number(parts[bestKey])) bestKey = k;
      }
      next[bestKey] = round3Offline(Math.max(0, Number(next[bestKey]) + drift));
    }
    return next;
  }

  function buildClientUnattributedSlice(source, clientTtft, clientE2e, sliceName, sliceNote) {
    if (clientTtft === null && clientE2e === null) return null;
    const ttftStageMs = clientTtft !== null ? round3Offline(clientTtft) : null;
    const decMs = clientTtft !== null && clientE2e !== null ? round3Offline(Math.max(0, clientE2e - clientTtft)) : null;
    const totalStageMs = ttftStageMs !== null || decMs !== null ? round3Offline((ttftStageMs || 0) + (decMs || 0)) : null;
    return {
      available: true,
      source,
      epp_scheduling_ms: null,
      queue_time_ms: null,
      kv_transfer_ms: null,
      prefill_time_ms: null,
      engine_overhead_ms: null,
      decode_first_step_ms: null,
      prefill_engine_overhead_ms: null,
      decode_handoff_overhead_ms: null,
      gateway_overhead_ms: null,
      ttft_unattributed_ms: ttftStageMs,
      decode_time_ms: decMs,
      ttft_stage_ms: ttftStageMs,
      total_stage_ms: totalStageMs,
      client_ttft_ms: clientTtft,
      client_e2e_ms: clientE2e,
      server_ttft_ms: null,
      server_e2e_ms: null,
      server_ttft_clamped: false,
      caveat: null,
      slice_note: sliceNote || null,
      reconciliation: buildOfflineReconciliation(clientE2e, totalStageMs, sliceName),
      share_of_e2e_pct: {
        decode_time: totalStageMs > 0 && decMs !== null ? round1Offline((decMs / totalStageMs) * 100) : null,
        ttft_unattributed: totalStageMs > 0 && ttftStageMs !== null ? round1Offline((ttftStageMs / totalStageMs) * 100) : null,
      },
      share_of_ttft_pct: {
        epp_scheduling: null,
        queue_time: null,
        kv_transfer: null,
        prefill_time: null,
        engine_overhead: null,
        gateway_overhead: null,
      },
    };
  }

  function synthesizeOfflineSlice(sliceName, ctx) {
    const {
      src,
      steadyWin,
      summary,
      summaryByRole,
      derivedRoleSummary,
      router,
      steadySrvPts,
      steadyRolePts,
      warmupSec,
      isDisagg,
      hasRoleSep,
      hasKvTransfer,
    } = ctx;
    const clientTtft = nonnegFinite(statVal(steadyWin.ttft_ms || steadyWin.ttft, `${sliceName}_ms`));
    const clientE2e = nonnegFinite(statVal(steadyWin.e2e_ms || steadyWin.e2e, `${sliceName}_ms`));

    if (sliceName === "min") {
      return buildClientUnattributedSlice(
        src,
        clientTtft,
        clientE2e,
        "min",
        "Server stage histograms do not expose single-request minimums; showing exact client Min TTFT."
      );
    }

    if (src === "pd_pooled_fallback") {
      const agg = sliceName === "mean" ? "mean" : sliceName;
      const eppMs = sliceName === "mean"
        ? eppAggVal(steadySrvPts, router, ["router_scheduler_latency_mean_ms", "router_scheduler_e2e_mean_ms"], ["scheduler_latency_mean_ms", "scheduler_e2e_mean_ms"], ["mean", "avg"], warmupSec, "mean")
        : (() => {
            const statKeys = sliceName === "p99" ? ["p99", "max", "mean", "p95", "p90"] : ["max", "p99", "mean", "p95", "p90"];
            return firstFinite(
              eppAggVal(steadySrvPts, router, ["router_scheduler_latency_p99_ms", "router_scheduler_e2e_p99_ms"], ["scheduler_latency_p99_ms", "scheduler_e2e_p99_ms"], statKeys, warmupSec, agg),
              eppAggVal(steadySrvPts, router, ["router_scheduler_latency_p95_ms", "router_scheduler_e2e_p95_ms"], ["scheduler_latency_p95_ms", "scheduler_e2e_p95_ms"], statKeys, warmupSec, agg),
              eppAggVal(steadySrvPts, router, ["router_scheduler_latency_p90_ms", "router_scheduler_e2e_p90_ms"], ["scheduler_latency_p90_ms", "scheduler_e2e_p90_ms"], statKeys, warmupSec, agg)
            );
          })();
      const nixlKey = sliceName === "mean" ? "nixl_transfer_latency_mean_ms" : "nixl_transfer_latency_p99_ms";
      const nixlMs = srvAggVal(steadySrvPts, summary, [nixlKey], nixlKey, warmupSec, agg);
      const res = computeLegacyStageSlice({
        source: "pd_pooled_fallback",
        quantile: sliceName,
        eppMs,
        kvTransferMs: nixlMs,
        clientTtftMs: clientTtft,
        clientE2eMs: clientE2e,
      });
      if (!res || !res.available) return null;
      if (sliceName === "max") {
        res.slice_note = "Server histograms do not report per-request max; stages reflect peak-window P99 bounds reconciled to exact client Max TTFT.";
      }
      return res;
    }

    if (src !== "server_telemetry") return null;

    if (sliceName === "mean") {
      const meanKeys = ["vllm_queue_time_mean_ms", "vllm_prefill_time_mean_ms", "vllm_decode_time_mean_ms"];
      const hasMeanSrv = meanKeys.some((k) => summaryStatOffline(summary && summary[k], "mean") !== null)
        || steadySrvPts.some((pt) => pt && meanKeys.some((k) => nonnegFinite(pt[k]) !== null))
        || (isDisagg && hasRoleSep && ["prefill", "decode"].some((r) => meanKeys.some((k) => roleAggVal(steadyRolePts, summaryByRole, derivedRoleSummary, r, k, warmupSec, "mean") !== null)));
      if (!hasMeanSrv) {
        return buildClientUnattributedSlice(
          "client_fallback",
          clientTtft,
          clientE2e,
          "mean",
          "Server mean stage histograms were not recorded; showing exact client Mean TTFT."
        );
      }
    }

    const agg = sliceName === "mean" ? "mean" : sliceName;
    const useRolePd = isDisagg && hasRoleSep;
    const statKeys = sliceName === "mean"
      ? ["mean", "avg"]
      : (sliceName === "p99" ? ["p99", "max", "mean", "p95", "p90"] : ["max", "p99", "mean", "p95", "p90"]);
    const eppMs = sliceName === "mean"
      ? eppAggVal(steadySrvPts, router, ["router_scheduler_latency_mean_ms", "router_scheduler_e2e_mean_ms"], ["scheduler_latency_mean_ms", "scheduler_e2e_mean_ms"], statKeys, warmupSec, "mean")
      : firstFinite(
          eppAggVal(steadySrvPts, router, ["router_scheduler_latency_p99_ms", "router_scheduler_e2e_p99_ms"], ["scheduler_latency_p99_ms", "scheduler_e2e_p99_ms"], statKeys, warmupSec, agg),
          eppAggVal(steadySrvPts, router, ["router_scheduler_latency_p95_ms", "router_scheduler_e2e_p95_ms"], ["scheduler_latency_p95_ms", "scheduler_e2e_p95_ms"], statKeys, warmupSec, agg),
          eppAggVal(steadySrvPts, router, ["router_scheduler_latency_p90_ms", "router_scheduler_e2e_p90_ms"], ["scheduler_latency_p90_ms", "scheduler_e2e_p90_ms"], statKeys, warmupSec, agg)
        );

    const primarySuffix = sliceName === "mean" ? "mean" : "p99";
    const fallbackSuffix = sliceName === "mean" ? null : "p90";
    let usedP90StageFallback = false;

    const srvMetric = (prefix) => {
      const k1 = `${prefix}_${primarySuffix}_ms`;
      const v1 = srvAggVal(steadySrvPts, summary, [k1], k1, warmupSec, agg);
      if (v1 !== null || !fallbackSuffix) return v1;
      const k2 = `${prefix}_${fallbackSuffix}_ms`;
      const v2 = srvAggVal(steadySrvPts, summary, [k2], k2, warmupSec, agg);
      if (v2 !== null) usedP90StageFallback = true;
      return v2;
    };
    const roleMetric = (role, prefix) => {
      const k1 = `${prefix}_${primarySuffix}_ms`;
      const v1 = roleAggVal(steadyRolePts, summaryByRole, derivedRoleSummary, role, k1, warmupSec, agg);
      if (v1 !== null || !fallbackSuffix) return v1;
      const k2 = `${prefix}_${fallbackSuffix}_ms`;
      const v2 = roleAggVal(steadyRolePts, summaryByRole, derivedRoleSummary, role, k2, warmupSec, agg);
      if (v2 !== null) usedP90StageFallback = true;
      return v2;
    };

    let queueMs = null;
    let kvTransferMs = null;
    let prefillMs = null;
    let decodeMs = null;
    let serverTtft = null;
    let serverE2e = null;
    let pTtft = null;
    let dTtft = null;
    let pQueueEff = null;
    let dQueueEff = null;
    let pPrefill = null;
    let dPrefill = null;

    if (useRolePd) {
      const nixlKey = `nixl_transfer_latency_${primarySuffix}_ms`;
      const nixlMs = hasKvTransfer ? srvAggVal(steadySrvPts, summary, [nixlKey], nixlKey, warmupSec, agg) : null;
      const pQRaw = roleMetric("prefill", "vllm_queue_time");
      const pQMean = roleAggVal(steadyRolePts, summaryByRole, derivedRoleSummary, "prefill", "vllm_queue_time_mean_ms", warmupSec, "mean");
      const dQRaw = roleMetric("decode", "vllm_queue_time");
      const dQMean = roleAggVal(steadyRolePts, summaryByRole, derivedRoleSummary, "decode", "vllm_queue_time_mean_ms", warmupSec, "mean");
      pQueueEff = sliceName === "mean" ? (pQMean !== null ? pQMean : pQRaw) : effectiveOfflineRoleQueue(pQRaw, pQMean, sliceName);
      dQueueEff = sliceName === "mean" ? (dQMean !== null ? dQMean : dQRaw) : effectiveOfflineRoleQueue(dQRaw, dQMean, sliceName);

      if (nixlMs !== null) {
        if (dQueueEff !== null) {
          kvTransferMs = round3Offline(Math.min(nixlMs, dQueueEff));
          const dQExclKv = round3Offline(Math.max(0, dQueueEff - kvTransferMs));
          queueMs = round3Offline((pQueueEff || 0) + dQExclKv);
        } else {
          const canonQ = srvMetric("vllm_queue_time");
          if (pQueueEff !== null) {
            kvTransferMs = round3Offline(nixlMs);
            queueMs = round3Offline(pQueueEff);
          } else if (canonQ !== null) {
            kvTransferMs = round3Offline(Math.min(nixlMs, canonQ));
            queueMs = round3Offline(Math.max(0, canonQ - kvTransferMs));
          } else {
            kvTransferMs = round3Offline(nixlMs);
          }
        }
      } else if (pQueueEff !== null || dQueueEff !== null) {
        queueMs = round3Offline((pQueueEff || 0) + (dQueueEff || 0));
      } else {
        queueMs = srvMetric("vllm_queue_time");
      }

      pPrefill = roleMetric("prefill", "vllm_prefill_time");
      prefillMs = pPrefill !== null ? pPrefill : srvMetric("vllm_prefill_time");
      dPrefill = roleMetric("decode", "vllm_prefill_time");
      decodeMs = roleMetric("decode", "vllm_decode_time");
      if (decodeMs === null) decodeMs = srvMetric("vllm_decode_time");
      pTtft = roleAggVal(steadyRolePts, summaryByRole, derivedRoleSummary, "prefill", `vllm_ttft_${primarySuffix}_ms`, warmupSec, agg);
      dTtft = roleAggVal(steadyRolePts, summaryByRole, derivedRoleSummary, "decode", `vllm_ttft_${primarySuffix}_ms`, warmupSec, agg);
      serverTtft = srvAggVal(steadySrvPts, summary, [`vllm_ttft_${primarySuffix}_ms`], `vllm_ttft_${primarySuffix}_ms`, warmupSec, agg);
      if (serverTtft === null && (pTtft !== null || dTtft !== null)) {
        serverTtft = round3Offline((pTtft || 0) + (dTtft || 0));
      }
      serverE2e = srvAggVal(steadySrvPts, summary, [`vllm_e2e_${primarySuffix}_ms`], `vllm_e2e_${primarySuffix}_ms`, warmupSec, agg);
    } else {
      const rawQ = srvMetric("vllm_queue_time");
      const nixlKey = `nixl_transfer_latency_${primarySuffix}_ms`;
      const nixlMs = hasKvTransfer ? srvAggVal(steadySrvPts, summary, [nixlKey], nixlKey, warmupSec, agg) : null;
      if (hasKvTransfer && nixlMs !== null) {
        if (rawQ !== null) {
          kvTransferMs = round3Offline(Math.min(nixlMs, rawQ));
          queueMs = round3Offline(Math.max(0, rawQ - kvTransferMs));
        } else {
          kvTransferMs = round3Offline(nixlMs);
          queueMs = null;
        }
      } else {
        queueMs = rawQ;
      }
      prefillMs = srvMetric("vllm_prefill_time");
      decodeMs = srvMetric("vllm_decode_time");
      serverTtft = srvAggVal(steadySrvPts, summary, [`vllm_ttft_${primarySuffix}_ms`], `vllm_ttft_${primarySuffix}_ms`, warmupSec, agg);
      serverE2e = srvAggVal(steadySrvPts, summary, [`vllm_e2e_${primarySuffix}_ms`], `vllm_e2e_${primarySuffix}_ms`, warmupSec, agg);
    }

    if (queueMs === null && kvTransferMs === null && prefillMs === null && decodeMs === null) {
      return null;
    }

    let effEpp = eppMs;
    if (sliceName === "p99" || sliceName === "max") {
      const guarded = normalizeNonAdditiveOverAlloc(
        { epp: eppMs, queue: queueMs, kv: kvTransferMs, prefill: prefillMs },
        clientTtft
      );
      effEpp = guarded.epp;
      queueMs = guarded.queue;
      kvTransferMs = guarded.kv;
      prefillMs = guarded.prefill;
    }

    const knownEngineTtft = (queueMs || 0) + (kvTransferMs || 0) + (prefillMs || 0);
    const hasEngineTtft = queueMs !== null || kvTransferMs !== null || prefillMs !== null;
    const hasRoleTtft = useRolePd && (pTtft !== null || dTtft !== null);
    const serverTtftClamped = sliceName !== "mean"
      && (hasRoleTtft ? ((pTtft || 0) >= 9500 || (dTtft || 0) >= 9500) : ((serverTtft || 0) >= 9500))
      && clientTtft !== null
      && serverTtft !== null
      && clientTtft > serverTtft + 500;
    const unboundedMaxTail = sliceName === "max"
      && (serverTtftClamped || serverTtft === null || (clientTtft !== null && clientTtft > serverTtft + 500));

    let engineOvMs = null;
    let gatewayMs = null;
    let sliceCaveat = null;
    if (clientTtft !== null && hasEngineTtft) {
      const totalResidual = round3Offline(Math.max(0, clientTtft - ((effEpp || 0) + knownEngineTtft)));
      if (serverTtftClamped || unboundedMaxTail) {
        engineOvMs = totalResidual;
        gatewayMs = null;
        if (serverTtftClamped) {
          sliceCaveat = "Server TTFT quantile hit the 10 s histogram ceiling; unmeasured engine tail is attributed to Server TTFT Residual (see Mean slice for exact additive attribution).";
        }
      } else if (serverTtft !== null) {
        engineOvMs = round3Offline(Math.min(totalResidual, Math.max(0, serverTtft - knownEngineTtft)));
        gatewayMs = round3Offline(Math.max(0, totalResidual - engineOvMs));
      } else {
        engineOvMs = null;
        gatewayMs = totalResidual;
      }
    } else if (serverTtft !== null && !serverTtftClamped && hasEngineTtft) {
      engineOvMs = round3Offline(Math.max(0, serverTtft - knownEngineTtft));
    }

    let decodeFirstStep = null;
    let prefillEngineOverhead = null;
    let decodeHandoffOverhead = null;
    if (useRolePd && !serverTtftClamped && !unboundedMaxTail) {
      const reconciled = reconcileOfflinePdEngineOverhead({
        engineOverheadMs: engineOvMs,
        serverTtftClamped,
        pTtft,
        pQueueEff,
        pPrefill,
        dTtft,
        dQueueEff,
        dPrefill,
      });
      decodeFirstStep = reconciled.decode_first_step_ms;
      prefillEngineOverhead = reconciled.prefill_engine_overhead_ms;
      decodeHandoffOverhead = reconciled.decode_handoff_overhead_ms;
    }

    const ttftParts = [effEpp, queueMs, kvTransferMs, prefillMs, engineOvMs, gatewayMs].filter((v) => v !== null);
    const ttftStageMs = ttftParts.length ? round3Offline(ttftParts.reduce((a, b) => a + b, 0)) : null;
    const totalParts = [effEpp, queueMs, kvTransferMs, prefillMs, engineOvMs, gatewayMs, decodeMs].filter((v) => v !== null);
    const totalStageMs = totalParts.length ? round3Offline(totalParts.reduce((a, b) => a + b, 0)) : null;

    const shareE2e = {};
    for (const [k, v] of [
      ["epp_scheduling", effEpp],
      ["queue_time", queueMs],
      ["kv_transfer", kvTransferMs],
      ["prefill_time", prefillMs],
      ["engine_overhead", engineOvMs],
      ["gateway_overhead", gatewayMs],
      ["decode_time", decodeMs],
    ]) {
      shareE2e[k] = totalStageMs !== null && totalStageMs > 0 && v !== null ? round1Offline((v / totalStageMs) * 100) : null;
    }
    const shareTtft = {};
    for (const [k, v] of [
      ["epp_scheduling", effEpp],
      ["queue_time", queueMs],
      ["kv_transfer", kvTransferMs],
      ["prefill_time", prefillMs],
      ["engine_overhead", engineOvMs],
      ["gateway_overhead", gatewayMs],
    ]) {
      shareTtft[k] = ttftStageMs !== null && ttftStageMs > 0 && v !== null ? round1Offline((v / ttftStageMs) * 100) : null;
    }

    let sliceNote = null;
    if (sliceName === "max") {
      sliceNote = "Server histograms do not report per-request max; stages reflect peak-window P99 bounds reconciled to exact client Max TTFT.";
    } else if (sliceName === "p99" && usedP90StageFallback) {
      sliceNote = "Prefill/Queue P99 estimated from peak-window P90 histograms (re-run to collect native P99 stage histograms).";
    }

    return {
      available: true,
      source: "server_telemetry",
      epp_scheduling_ms: effEpp,
      queue_time_ms: queueMs,
      kv_transfer_ms: kvTransferMs,
      prefill_time_ms: prefillMs,
      engine_overhead_ms: engineOvMs,
      decode_first_step_ms: decodeFirstStep,
      prefill_engine_overhead_ms: prefillEngineOverhead,
      decode_handoff_overhead_ms: decodeHandoffOverhead,
      gateway_overhead_ms: gatewayMs,
      ttft_unattributed_ms: null,
      decode_time_ms: decodeMs,
      ttft_stage_ms: ttftStageMs,
      total_stage_ms: totalStageMs,
      client_ttft_ms: clientTtft,
      client_e2e_ms: clientE2e,
      server_ttft_ms: serverTtft ?? null,
      server_e2e_ms: serverE2e ?? null,
      server_ttft_clamped: serverTtftClamped,
      caveat: sliceCaveat,
      slice_note: sliceNote,
      reconciliation: buildOfflineReconciliation(clientE2e, totalStageMs, sliceName),
      share_of_e2e_pct: shareE2e,
      share_of_ttft_pct: shareTtft,
    };
  }

  function buildOfflineStageLatencyView(f, st, cs, srvPts) {
    const slb = f && f.stage_latency_breakdown && typeof f.stage_latency_breakdown === "object"
      ? f.stage_latency_breakdown
      : null;
    const legacyPooled = isLegacyPooledPd(f, st);
    const summary = (st && st.summary) || {};
    const summaryByRole = (st && st.summary_by_role) || null;
    const derivedRoleSummary = (st && st.derived && st.derived.role_summary) || null;
    const router = (st && st.router) || {};
    const steadyWin = resolveSteadyClientWindow(cs);
    const warmupSec = firstFinite(cs && cs.warmup_seconds, state.data && state.data.task && state.data.task.warmup_seconds, 0) || 0;
    const originEpoch = resolveServerOriginEpoch(st, cs);
    const steadySrvPts = filterSteadyOfflinePts(srvPts, originEpoch, warmupSec, true);
    const steadyRolePts = filterSteadyOfflinePts(st && st.role_series, originEpoch, warmupSec, false);
    const isServerAvailable = Boolean(st && st.status !== "unavailable" && st.available !== false && (st.status === "available" || st.available === true));
    const isDisagg = String((st && st.topology_mode) || "").toLowerCase() === "disaggregated";
    const hasRoleSep = hasRoleSeparatedLatency(st);

    if (slb && slb.available !== undefined) {
      if (slb.available === false) {
        return {
          available: false,
          source: slb.source || "unavailable",
          isLegacyPooledPd: false,
          hasP90: false,
          hasKvTransfer: false,
          caveat: slb.caveat || null,
          roleBreakdown: null,
          primarySlice: null,
          slices: {},
          dominantE2eStage: null,
          dominantTtftStage: null,
          dominantE2eStageP90: null,
          dominantTtftStageP90: null,
          peakWindowShift: null,
          series: [],
        };
      }
      const slices = {
        min: normalizeOfflineV6StageSlice(slb.min || null, "min"),
        p50: normalizeOfflineV6StageSlice(slb.p50 || null, "p50"),
        mean: normalizeOfflineV6StageSlice(slb.mean || null, "mean"),
        p90: normalizeOfflineV6StageSlice(slb.p90 || null, "p90"),
        p99: normalizeOfflineV6StageSlice(slb.p99 || null, "p99"),
        max: normalizeOfflineV6StageSlice(slb.max || null, "max"),
      };
      const series = Array.isArray(slb.series) ? slb.series : [];
      const src = slb.source || (slb.available ? "server_telemetry" : "unavailable");
      if (slices.mean && slices.mean.available && slices.mean.source === "client_fallback" && !isFiniteMetric(slices.mean.ttft_unattributed_ms)) {
        const meanFallbackVal = nonnegFinite(firstFinite(slices.mean.ttft_stage_ms, slices.mean.client_ttft_ms));
        if (meanFallbackVal !== null) {
          slices.mean = {
            ...slices.mean,
            ttft_unattributed_ms: meanFallbackVal,
            slice_note: slices.mean.slice_note || (src === "server_telemetry" ? "Server mean stage histograms were not recorded; showing exact client Mean TTFT." : null),
          };
        }
      }
      const hasKvTransfer = Boolean(
        slb.has_kv_transfer_series
        || isFiniteMetric(slices.p50 && slices.p50.kv_transfer_ms)
        || isFiniteMetric(slices.p90 && slices.p90.kv_transfer_ms)
        || isFiniteMetric(slices.mean && slices.mean.kv_transfer_ms)
        || isFiniteMetric(slices.p99 && slices.p99.kv_transfer_ms)
        || isFiniteMetric(slices.max && slices.max.kv_transfer_ms)
        || series.some((pt) => pt && (isFiniteMetric(pt.kv_transfer_ms) || isFiniteMetric(pt.kv_transfer_p90_ms)))
        || (isDisagg && (
          summaryStatOffline(summary.nixl_transfer_latency_p50_ms, "mean") !== null
          || summaryStatOffline(summary.nixl_transfer_latency_p90_ms, "mean") !== null
          || summaryStatOffline(summary.nixl_transfer_latency_p99_ms, "p99") !== null
        ))
      );
      if (hasServerLatencyTelemetry(st) && hasClientSteadyWindow(cs) && (src === "server_telemetry" || src === "pd_pooled_fallback")) {
        const synthCtx = {
          src,
          steadyWin,
          summary,
          summaryByRole,
          derivedRoleSummary,
          router,
          steadySrvPts,
          steadyRolePts,
          warmupSec,
          isDisagg,
          hasRoleSep,
          hasKvTransfer,
        };
        for (const key of ["min", "mean", "p99", "max"]) {
          if (!slices[key] || slices[key].available === false) {
            const synth = synthesizeOfflineSlice(key, synthCtx);
            if (synth && synth.available) slices[key] = synth;
          }
        }
      }
      const hasP90 = Boolean(slb.has_p90_series)
        || Boolean(slices.p90 && slices.p90.available)
        || series.some((pt) => pt && (
          isFiniteMetric(pt.queue_p90_ms)
          || isFiniteMetric(pt.kv_transfer_p90_ms)
          || isFiniteMetric(pt.prefill_p90_ms)
          || isFiniteMetric(pt.decode_p90_ms)
          || isFiniteMetric(pt.epp_sched_p90_ms)
          || isFiniteMetric(pt.ttft_unattributed_p90_ms)
          || isFiniteMetric(pt.ttft_p90_ms)
        ));
      const hoistedCaveat = slb.caveat
        || (["p50", "p90", "p99", "max", "mean"].map((k) => slices[k] && slices[k].caveat).find(Boolean))
        || null;
      return {
        available: Boolean(slb.available),
        source: src,
        isLegacyPooledPd: src === "pd_pooled_fallback" || legacyPooled,
        hasP90,
        hasKvTransfer,
        caveat: hoistedCaveat,
        roleBreakdown: slb.role_breakdown || null,
        primarySlice: slb.primary_slice || (slices.p50 && slices.p50.available ? "p50" : slices.mean && slices.mean.available ? "mean" : slices.p90 && slices.p90.available ? "p90" : null),
        slices,
        dominantE2eStage: slb.dominant_e2e_stage || null,
        dominantTtftStage: slb.dominant_ttft_stage || null,
        dominantE2eStageP90: slb.dominant_e2e_stage_p90 || null,
        dominantTtftStageP90: slb.dominant_ttft_stage_p90 || null,
        peakWindowShift: slb.peak_window_shift || null,
        series,
      };
    }

    if (legacyPooled) {
      const buckets = Array.isArray(cs && (cs.buckets || cs.timeseries)) ? (cs.buckets || cs.timeseries) : [];
      const maxDist = Math.max(5, Number((st && st.step_seconds) || 5), Number((cs && cs.bucket_seconds) || 5));
      const pdSeries = buckets.map((b) => {
        const t = Number(b.mid_seconds ?? b.time_seconds ?? b.start_seconds ?? b.t ?? 0);
        const srv = findNearestOfflineSrvPt(srvPts, t, maxDist);
        const t50 = nonnegFinite(statVal(b.ttft || b.ttft_ms, "p50") ?? b.ttft_p50_ms);
        const e50 = nonnegFinite(statVal(b.e2e || b.e2e_ms, "p50") ?? b.e2e_p50_ms);
        const epp50 = srv ? nonnegFinite(firstFinite(srv.router_scheduler_latency_p50_ms, srv.router_scheduler_e2e_p50_ms)) : null;
        const rawNixl50 = srv ? nonnegFinite(srv.nixl_transfer_latency_p50_ms) : null;
        const kv50 = rawNixl50 !== null && t50 !== null ? Math.min(rawNixl50, Math.max(0, t50 - (epp50 || 0))) : rawNixl50;
        const ttftU50 = t50 !== null ? Math.max(0, t50 - ((epp50 || 0) + (kv50 || 0))) : null;

        const t90 = nonnegFinite(statVal(b.ttft || b.ttft_ms, "p90") ?? b.ttft_p90_ms);
        const e90 = nonnegFinite(statVal(b.e2e || b.e2e_ms, "p90") ?? b.e2e_p90_ms);
        const epp90 = srv ? nonnegFinite(firstFinite(srv.router_scheduler_latency_p90_ms, srv.router_scheduler_e2e_p90_ms)) : null;
        const rawNixl90 = srv ? nonnegFinite(srv.nixl_transfer_latency_p90_ms) : null;
        const kv90 = rawNixl90 !== null && t90 !== null ? Math.min(rawNixl90, Math.max(0, t90 - (epp90 || 0))) : rawNixl90;
        const ttftU90 = t90 !== null ? Math.max(0, t90 - ((epp90 || 0) + (kv90 || 0))) : null;
        return {
          t,
          epp_sched_ms: epp50,
          kv_transfer_ms: kv50,
          ttft_unattributed_ms: ttftU50,
          decode_ms: t50 !== null && e50 !== null ? Math.max(0, e50 - t50) : null,
          epp_sched_p90_ms: epp90,
          kv_transfer_p90_ms: kv90,
          ttft_unattributed_p90_ms: ttftU90,
          decode_p90_ms: t90 !== null && e90 !== null ? Math.max(0, e90 - t90) : null,
        };
      });
      const p50Slice = computeLegacyStageSlice({
        source: "pd_pooled_fallback",
        quantile: "p50",
        eppMs: firstFinite(routerStatVal(router, "scheduler_latency_p50_ms", "scheduler_e2e_p50_ms", "p50"), meanFinite(pdSeries.map((pt) => pt.epp_sched_ms))),
        kvTransferMs: firstFinite(podStatMean(summary, "nixl_transfer_latency_p50_ms"), meanFinite(pdSeries.map((pt) => pt.kv_transfer_ms))),
        clientTtftMs: nonnegFinite(statVal(steadyWin.ttft_ms || steadyWin.ttft, "p50_ms")),
        clientE2eMs: nonnegFinite(statVal(steadyWin.e2e_ms || steadyWin.e2e, "p50_ms")),
      });
      const p90Slice = computeLegacyStageSlice({
        source: "pd_pooled_fallback",
        quantile: "p90",
        eppMs: firstFinite(routerStatVal(router, "scheduler_latency_p90_ms", "scheduler_e2e_p90_ms", "p90"), routerStatVal(router, "scheduler_latency_p95_ms", null, "p95"), meanFinite(pdSeries.map((pt) => pt.epp_sched_p90_ms))),
        kvTransferMs: firstFinite(podStatMean(summary, "nixl_transfer_latency_p90_ms"), meanFinite(pdSeries.map((pt) => pt.kv_transfer_p90_ms))),
        clientTtftMs: nonnegFinite(statVal(steadyWin.ttft_ms || steadyWin.ttft, "p90_ms")),
        clientE2eMs: nonnegFinite(statVal(steadyWin.e2e_ms || steadyWin.e2e, "p90_ms")),
      });
      const pdSynthCtx = {
        src: "pd_pooled_fallback",
        steadyWin,
        summary,
        summaryByRole,
        derivedRoleSummary,
        router,
        steadySrvPts,
        steadyRolePts,
        warmupSec,
        isDisagg,
        hasRoleSep,
        hasKvTransfer: true,
      };
      const minSlice = synthesizeOfflineSlice("min", pdSynthCtx);
      const meanSlice = synthesizeOfflineSlice("mean", pdSynthCtx);
      const p99Slice = synthesizeOfflineSlice("p99", pdSynthCtx);
      const maxSlice = synthesizeOfflineSlice("max", pdSynthCtx);
      if (p50Slice.available || p90Slice.available || (meanSlice && meanSlice.available) || pdSeries.some((pt) => isFiniteMetric(pt.ttft_unattributed_ms) || isFiniteMetric(pt.decode_ms))) {
        const pdDomE2e = (sl) => {
          if (!sl || !sl.available) return null;
          const dec = sl.decode_time_ms ?? -1;
          const ttftU = sl.ttft_unattributed_ms ?? -1;
          const kv = sl.kv_transfer_ms ?? -1;
          if (dec > ttftU && dec > kv) return "decode_time";
          if (kv > dec && kv > ttftU) return "kv_transfer";
          return null;
        };
        const pdDomTtft = (sl) => (sl && sl.share_of_ttft_pct && (sl.share_of_ttft_pct.kv_transfer || 0) >= 50 ? "kv_transfer" : null);
        const primaryKey = p50Slice.available ? "p50" : p90Slice.available ? "p90" : "mean";
        const primaryObj = primaryKey === "p50" ? p50Slice : primaryKey === "p90" ? p90Slice : meanSlice;
        const hasPdKv = isFiniteMetric(p50Slice.kv_transfer_ms)
          || isFiniteMetric(p90Slice.kv_transfer_ms)
          || isFiniteMetric(meanSlice && meanSlice.kv_transfer_ms)
          || isFiniteMetric(p99Slice && p99Slice.kv_transfer_ms)
          || isFiniteMetric(maxSlice && maxSlice.kv_transfer_ms)
          || pdSeries.some((pt) => pt && (isFiniteMetric(pt.kv_transfer_ms) || isFiniteMetric(pt.kv_transfer_p90_ms)));
        return {
          available: true,
          source: "pd_pooled_fallback",
          isLegacyPooledPd: true,
          hasP90: Boolean(p90Slice.available) || pdSeries.some((pt) => isFiniteMetric(pt.ttft_unattributed_p90_ms) || isFiniteMetric(pt.decode_p90_ms)),
          hasKvTransfer: hasPdKv,
          caveat: OFFLINE_PD_POOLED_CAVEAT,
          roleBreakdown: null,
          primarySlice: primaryKey,
          slices: {
            min: minSlice,
            p50: p50Slice,
            mean: meanSlice && meanSlice.available ? meanSlice : null,
            p90: p90Slice,
            p99: p99Slice,
            max: maxSlice,
          },
          dominantE2eStage: pdDomE2e(primaryObj),
          dominantTtftStage: pdDomTtft(primaryObj),
          dominantE2eStageP90: p90Slice.available ? pdDomE2e(p90Slice) : null,
          dominantTtftStageP90: p90Slice.available ? pdDomTtft(p90Slice) : null,
          peakWindowShift: null,
          series: pdSeries,
        };
      }
    }

    const hasServerStage = isServerAvailable && (
      srvPts.some((p) =>
        isFiniteMetric(p.vllm_queue_time_p50_ms)
        || isFiniteMetric(p.vllm_prefill_time_p50_ms)
        || isFiniteMetric(p.vllm_decode_time_p50_ms)
        || isFiniteMetric(p.vllm_queue_time_p90_ms)
        || isFiniteMetric(p.vllm_prefill_time_p90_ms)
        || isFiniteMetric(p.vllm_decode_time_p90_ms)
      )
      || isFiniteMetric(podStatMean(summary, "vllm_queue_time_p50_ms"))
      || isFiniteMetric(podStatMean(summary, "vllm_prefill_time_p50_ms"))
      || isFiniteMetric(podStatMean(summary, "vllm_decode_time_p50_ms"))
      || isFiniteMetric(podStatMean(summary, "vllm_queue_time_p90_ms"))
      || isFiniteMetric(podStatMean(summary, "vllm_prefill_time_p90_ms"))
      || isFiniteMetric(podStatMean(summary, "vllm_decode_time_p90_ms"))
    );

    if (hasServerStage) {
      const hasKvTransfer = isDisagg && (
        isFiniteMetric(podStatMean(summary, "nixl_transfer_latency_p50_ms"))
        || isFiniteMetric(podStatMean(summary, "nixl_transfer_latency_p90_ms"))
        || isFiniteMetric(podStatMean(summary, "nixl_transfer_latency_mean_ms"))
        || summaryStatOffline(summary.nixl_transfer_latency_p99_ms, "p99") !== null
        || srvPts.some((p) => isFiniteMetric(p.nixl_transfer_latency_p50_ms) || isFiniteMetric(p.nixl_transfer_latency_p90_ms))
      );
      const series = srvPts.map((p) => {
        const rawQ50 = nonnegFinite(p.vllm_queue_time_p50_ms);
        const rawNixl50 = hasKvTransfer ? nonnegFinite(p.nixl_transfer_latency_p50_ms) : null;
        const kv50 = hasKvTransfer && rawNixl50 !== null ? (rawQ50 !== null ? Math.min(rawNixl50, rawQ50) : rawNixl50) : null;
        const q50 = hasKvTransfer && kv50 !== null && rawQ50 !== null ? Math.max(0, rawQ50 - kv50) : rawQ50;

        const rawQ90 = nonnegFinite(p.vllm_queue_time_p90_ms);
        const rawNixl90 = hasKvTransfer ? nonnegFinite(p.nixl_transfer_latency_p90_ms) : null;
        const kv90 = hasKvTransfer && rawNixl90 !== null ? (rawQ90 !== null ? Math.min(rawNixl90, rawQ90) : rawNixl90) : null;
        const q90 = hasKvTransfer && kv90 !== null && rawQ90 !== null ? Math.max(0, rawQ90 - kv90) : rawQ90;
        return {
          t: p._t,
          epp_sched_ms: nonnegFinite(firstFinite(p.router_scheduler_latency_p50_ms, p.router_scheduler_e2e_p50_ms)),
          queue_ms: q50,
          ...(hasKvTransfer ? { kv_transfer_ms: kv50 } : {}),
          prefill_ms: nonnegFinite(p.vllm_prefill_time_p50_ms),
          decode_ms: nonnegFinite(p.vllm_decode_time_p50_ms),
          epp_sched_p90_ms: nonnegFinite(firstFinite(p.router_scheduler_latency_p90_ms, p.router_scheduler_e2e_p90_ms)),
          queue_p90_ms: q90,
          ...(hasKvTransfer ? { kv_transfer_p90_ms: kv90 } : {}),
          prefill_p90_ms: nonnegFinite(p.vllm_prefill_time_p90_ms),
          decode_p90_ms: nonnegFinite(p.vllm_decode_time_p90_ms),
        };
      });
      const resolveSliceQueueAndKv = (suffix, rawQ, rawNixl) => {
        if (!hasKvTransfer || rawNixl === null) return { queueMs: rawQ, kvTransferMs: null };
        const dRaw = summaryByRole && summaryByRole.decode ? podStatMean(summaryByRole.decode, `vllm_queue_time_${suffix}_ms`) : null;
        if (dRaw !== null) {
          const kv = Math.min(rawNixl, dRaw);
          const pEff = (summaryByRole && summaryByRole.prefill ? podStatMean(summaryByRole.prefill, `vllm_queue_time_${suffix}_ms`) : null) || 0;
          return { queueMs: pEff + Math.max(0, dRaw - kv), kvTransferMs: kv };
        }
        if (rawQ !== null) {
          const kv = Math.min(rawNixl, rawQ);
          return { queueMs: Math.max(0, rawQ - kv), kvTransferMs: kv };
        }
        return { queueMs: null, kvTransferMs: rawNixl };
      };
      const cTtftP50 = nonnegFinite(statVal(steadyWin.ttft_ms || steadyWin.ttft, "p50_ms"));
      const cE2eP50 = nonnegFinite(statVal(steadyWin.e2e_ms || steadyWin.e2e, "p50_ms"));
      const cTtftP90 = nonnegFinite(statVal(steadyWin.ttft_ms || steadyWin.ttft, "p90_ms"));
      const cE2eP90 = nonnegFinite(statVal(steadyWin.e2e_ms || steadyWin.e2e, "p90_ms"));

      const qKv50 = resolveSliceQueueAndKv(
        "p50",
        firstFinite(podStatMean(summary, "vllm_queue_time_p50_ms"), meanFinite(series.map((pt) => pt.queue_ms))),
        hasKvTransfer ? firstFinite(podStatMean(summary, "nixl_transfer_latency_p50_ms"), meanFinite(series.map((pt) => pt.kv_transfer_ms))) : null
      );
      const p50Slice = computeLegacyStageSlice({
        source: "server_telemetry",
        quantile: "p50",
        eppMs: firstFinite(routerStatVal(router, "scheduler_latency_p50_ms", "scheduler_e2e_p50_ms", "p50"), meanFinite(series.map((pt) => pt.epp_sched_ms))),
        queueMs: qKv50.queueMs,
        kvTransferMs: qKv50.kvTransferMs,
        prefillMs: firstFinite(podStatMean(summary, "vllm_prefill_time_p50_ms"), meanFinite(series.map((pt) => pt.prefill_ms))),
        decodeMs: firstFinite(podStatMean(summary, "vllm_decode_time_p50_ms"), meanFinite(series.map((pt) => pt.decode_ms))),
        clientTtftMs: cTtftP50,
        clientE2eMs: cE2eP50,
        serverTtftMs: podStatMean(summary, "vllm_ttft_p50_ms"),
        serverE2eMs: podStatMean(summary, "vllm_e2e_p50_ms"),
      });

      const qKv90 = resolveSliceQueueAndKv(
        "p90",
        firstFinite(podStatMean(summary, "vllm_queue_time_p90_ms"), meanFinite(series.map((pt) => pt.queue_p90_ms))),
        hasKvTransfer ? firstFinite(podStatMean(summary, "nixl_transfer_latency_p90_ms"), meanFinite(series.map((pt) => pt.kv_transfer_p90_ms))) : null
      );
      const p90Slice = computeLegacyStageSlice({
        source: "server_telemetry",
        quantile: "p90",
        eppMs: firstFinite(routerStatVal(router, "scheduler_latency_p90_ms", "scheduler_e2e_p90_ms", "p90"), routerStatVal(router, "scheduler_latency_p95_ms", null, "p95"), meanFinite(series.map((pt) => pt.epp_sched_p90_ms))),
        queueMs: qKv90.queueMs,
        kvTransferMs: qKv90.kvTransferMs,
        prefillMs: firstFinite(podStatMean(summary, "vllm_prefill_time_p90_ms"), meanFinite(series.map((pt) => pt.prefill_p90_ms))),
        decodeMs: firstFinite(podStatMean(summary, "vllm_decode_time_p90_ms"), meanFinite(series.map((pt) => pt.decode_p90_ms))),
        clientTtftMs: cTtftP90,
        clientE2eMs: cE2eP90,
        serverTtftMs: podStatMean(summary, "vllm_ttft_p90_ms"),
        serverE2eMs: podStatMean(summary, "vllm_e2e_p90_ms"),
      });

      const srvSynthCtx = {
        src: "server_telemetry",
        steadyWin,
        summary,
        summaryByRole,
        derivedRoleSummary,
        router,
        steadySrvPts,
        steadyRolePts,
        warmupSec,
        isDisagg,
        hasRoleSep,
        hasKvTransfer,
      };
      const minSlice = synthesizeOfflineSlice("min", srvSynthCtx);
      const meanSlice = synthesizeOfflineSlice("mean", srvSynthCtx);
      const p99Slice = synthesizeOfflineSlice("p99", srvSynthCtx);
      const maxSlice = synthesizeOfflineSlice("max", srvSynthCtx);

      const primaryKey = p50Slice.available ? "p50" : p90Slice.available ? "p90" : (meanSlice && meanSlice.available ? "mean" : null);
      const primaryObj = primaryKey ? (primaryKey === "p50" ? p50Slice : primaryKey === "p90" ? p90Slice : meanSlice) : null;
      const e2eOrder = ["queue_time", "kv_transfer", "prefill_time", "decode_time", "engine_overhead", "gateway_overhead", "epp_scheduling"];
      const ttftOrder = ["queue_time", "kv_transfer", "prefill_time", "engine_overhead", "gateway_overhead", "epp_scheduling"];
      const hasP90 = Boolean(p90Slice.available) || series.some((pt) => isFiniteMetric(pt.queue_p90_ms) || isFiniteMetric(pt.kv_transfer_p90_ms) || isFiniteMetric(pt.prefill_p90_ms) || isFiniteMetric(pt.decode_p90_ms) || isFiniteMetric(pt.epp_sched_p90_ms));
      const hoistedCaveat = (["p50", "p90", "p99", "max", "mean"].map((k) => ({ p50: p50Slice, p90: p90Slice, p99: p99Slice, max: maxSlice, mean: meanSlice }[k] && { p50: p50Slice, p90: p90Slice, p99: p99Slice, max: maxSlice, mean: meanSlice }[k].caveat)).find(Boolean)) || null;
      return {
        available: Boolean(p50Slice.available || p90Slice.available || (meanSlice && meanSlice.available)),
        source: "server_telemetry",
        isLegacyPooledPd: false,
        hasP90,
        hasKvTransfer,
        caveat: hoistedCaveat,
        roleBreakdown: null,
        primarySlice: primaryKey,
        slices: {
          min: minSlice,
          p50: p50Slice,
          mean: meanSlice && meanSlice.available ? meanSlice : null,
          p90: p90Slice,
          p99: p99Slice,
          max: maxSlice,
        },
        dominantE2eStage: primaryObj ? dominantStageFromShare(primaryObj.share_of_e2e_pct, e2eOrder) : null,
        dominantTtftStage: primaryObj ? dominantStageFromShare(primaryObj.share_of_ttft_pct, ttftOrder) : null,
        dominantE2eStageP90: p90Slice.available ? dominantStageFromShare(p90Slice.share_of_e2e_pct, e2eOrder) : null,
        dominantTtftStageP90: p90Slice.available ? dominantStageFromShare(p90Slice.share_of_ttft_pct, ttftOrder) : null,
        peakWindowShift: null,
        series,
      };
    }

    const buckets = Array.isArray(cs && (cs.buckets || cs.timeseries)) ? (cs.buckets || cs.timeseries) : [];
    const cSeries = buckets.map((b) => {
      const t50 = nonnegFinite(statVal(b.ttft || b.ttft_ms, "p50") ?? b.ttft_p50_ms);
      const e50 = nonnegFinite(statVal(b.e2e || b.e2e_ms, "p50") ?? b.e2e_p50_ms);
      const t90 = nonnegFinite(statVal(b.ttft || b.ttft_ms, "p90") ?? b.ttft_p90_ms);
      const e90 = nonnegFinite(statVal(b.e2e || b.e2e_ms, "p90") ?? b.e2e_p90_ms);
      return {
        t: Number(b.mid_seconds ?? b.time_seconds ?? b.start_seconds ?? b.t ?? 0),
        ttft_ms: t50,
        decode_ms: t50 !== null && e50 !== null ? Math.max(0, e50 - t50) : null,
        ttft_p90_ms: t90,
        decode_p90_ms: t90 !== null && e90 !== null ? Math.max(0, e90 - t90) : null,
      };
    });
    const minSlice = computeLegacyStageSlice({
      source: "client_fallback",
      quantile: "min",
      clientTtftMs: nonnegFinite(statVal(steadyWin.ttft_ms || steadyWin.ttft, "min_ms")),
      clientE2eMs: nonnegFinite(statVal(steadyWin.e2e_ms || steadyWin.e2e, "min_ms")),
    });
    const p50Slice = computeLegacyStageSlice({
      source: "client_fallback",
      quantile: "p50",
      clientTtftMs: nonnegFinite(statVal(steadyWin.ttft_ms || steadyWin.ttft, "p50_ms")),
      clientE2eMs: nonnegFinite(statVal(steadyWin.e2e_ms || steadyWin.e2e, "p50_ms")),
    });
    const p90Slice = computeLegacyStageSlice({
      source: "client_fallback",
      quantile: "p90",
      clientTtftMs: nonnegFinite(statVal(steadyWin.ttft_ms || steadyWin.ttft, "p90_ms")),
      clientE2eMs: nonnegFinite(statVal(steadyWin.e2e_ms || steadyWin.e2e, "p90_ms")),
    });
    const meanSlice = computeLegacyStageSlice({
      source: "client_fallback",
      quantile: "mean",
      clientTtftMs: nonnegFinite(statVal(steadyWin.ttft_ms || steadyWin.ttft, "mean_ms")),
      clientE2eMs: nonnegFinite(statVal(steadyWin.e2e_ms || steadyWin.e2e, "mean_ms")),
    });
    const p99Slice = computeLegacyStageSlice({
      source: "client_fallback",
      quantile: "p99",
      clientTtftMs: nonnegFinite(statVal(steadyWin.ttft_ms || steadyWin.ttft, "p99_ms")),
      clientE2eMs: nonnegFinite(statVal(steadyWin.e2e_ms || steadyWin.e2e, "p99_ms")),
    });
    const maxSlice = computeLegacyStageSlice({
      source: "client_fallback",
      quantile: "max",
      clientTtftMs: nonnegFinite(statVal(steadyWin.ttft_ms || steadyWin.ttft, "max_ms")),
      clientE2eMs: nonnegFinite(statVal(steadyWin.e2e_ms || steadyWin.e2e, "max_ms")),
    });
    const available = Boolean(p50Slice.available || p90Slice.available || meanSlice.available || cSeries.some((pt) => pt.ttft_ms !== null || pt.decode_ms !== null));
    if (!available) {
      return { available: false, source: "unavailable", isLegacyPooledPd: false, hasP90: false, hasKvTransfer: false, caveat: null, primarySlice: null, slices: {}, peakWindowShift: null, series: [] };
    }
    const primarySlice = p50Slice.available ? "p50" : p90Slice.available ? "p90" : "mean";
    const domFor = (sl) => {
      if (!sl || !sl.available || !sl.share_of_e2e_pct) return null;
      const dec = firstFinite(sl.share_of_e2e_pct.decode_time);
      const ttft = firstFinite(sl.share_of_e2e_pct.ttft_unattributed);
      return dec !== null && dec > (ttft || 0) ? "decode_time" : null;
    };
    const hasP90 = Boolean(p90Slice.available) || cSeries.some((pt) => pt.ttft_p90_ms !== null || pt.decode_p90_ms !== null);
    return {
      available: true,
      source: "client_fallback",
      isLegacyPooledPd: false,
      hasP90,
      hasKvTransfer: false,
      caveat: null,
      roleBreakdown: null,
      primarySlice,
      slices: { min: minSlice, p50: p50Slice, mean: meanSlice, p90: p90Slice, p99: p99Slice, max: maxSlice },
      dominantE2eStage: domFor(primarySlice === "p50" ? p50Slice : primarySlice === "p90" ? p90Slice : meanSlice),
      dominantTtftStage: null,
      dominantE2eStageP90: domFor(p90Slice),
      dominantTtftStageP90: null,
      peakWindowShift: null,
      series: cSeries,
    };
  }

  function buildOfflineTtftDecomposition(f, slbView) {
    const dec = (f && f.ttft_decomposition) || {};
    const hasDecompObj = Boolean(f && f.ttft_decomposition && typeof f.ttft_decomposition === "object");
    const hasDec = Boolean(hasDecompObj && dec.available !== false);
    const hasExplicitDec = Boolean(
      hasDec && (
        (dec.min && dec.min.available !== false)
        || (dec.p50 && dec.p50.available !== false)
        || (dec.mean && dec.mean.available !== false)
        || (dec.p90 && dec.p90.available !== false)
        || (dec.p99 && dec.p99.available !== false)
        || (dec.max && dec.max.available !== false)
        || dec.vllm_prefill_p50_ms !== undefined || dec.vllm_queue_p50_ms !== undefined || dec.overhead_p50_ms !== undefined
        || dec.vllm_prefill_p90_ms !== undefined || dec.vllm_queue_p90_ms !== undefined || dec.overhead_p90_ms !== undefined
        || dec.prefill_ms !== undefined || dec.queue_ms !== undefined || dec.overhead_ms !== undefined
      )
    );
    const stageSource = slbView && slbView.source;
    if (
      (hasDecompObj && dec.available === false && stageSource !== "pd_pooled_fallback")
      || (!hasExplicitDec && stageSource !== "server_telemetry" && stageSource !== "pd_pooled_fallback")
    ) {
      return {
        source: stageSource || (hasDecompObj && dec.source) || "unavailable",
        slices: [],
        hasKvTransfer: false,
        hasEppBar: false,
        hasEngineResidual: false,
        residualLabel: "Client / Gateway / Routing",
        unsplitEngineLabel: "Engine / API Overhead",
        segDefs: [],
        caveat: null,
        reason: dec.reason,
      };
    }

    const slbSource = f && f.stage_latency_breakdown && f.stage_latency_breakdown.source;
    const useStageFallback = slbView && slbView.source === "pd_pooled_fallback" && dec.source !== "pd_pooled_fallback"
      && (slbSource === "pd_pooled_fallback" || !hasExplicitDec);
    const source = useStageFallback
      ? "pd_pooled_fallback"
      : (dec.source || (slbView && slbView.source === "pd_pooled_fallback" && !hasExplicitDec ? "pd_pooled_fallback" : "server_telemetry"));
    const roleBreakdown = (slbView && slbView.roleBreakdown) || (f && f.stage_latency_breakdown && f.stage_latency_breakdown.role_breakdown) || null;

    const extract = (k) => {
      const rawDs = !useStageFallback && hasDec && dec[k] && typeof dec[k] === "object" ? dec[k] : null;
      const ds = rawDs && rawDs.available !== false ? rawDs : null;
      const rawSs = (
        slbView
        && slbView.slices
        && slbView.slices[k]
        && slbView.slices[k].available !== false
        && (source === "pd_pooled_fallback" || (slbView.slices[k].source || slbView.source) !== "pd_pooled_fallback")
      )
        ? slbView.slices[k]
        : null;
      const ss = rawSs ? normalizeOfflineV6StageSlice(rawSs, k) : null;
      const hasFlat = !ds && !ss && !useStageFallback && hasDec
        && (dec[`vllm_prefill_${k}_ms`] !== undefined
          || dec[`vllm_queue_${k}_ms`] !== undefined
          || dec[`overhead_${k}_ms`] !== undefined
          || dec[`kv_transfer_${k}_ms`] !== undefined
          || dec[`epp_scheduler_${k}_ms`] !== undefined
          || dec[`engine_overhead_${k}_ms`] !== undefined
          || dec[`ttft_unattributed_${k}_ms`] !== undefined);
      if (!ds && !ss && !hasFlat) return null;

      let prefill;
      let queue;
      let kvTransfer;
      let epp;
      let engOv;
      let gwOv;
      let ttftUnattr;
      let clientTtftMs = nonnegFinite(firstFinite(
        ds && ds.client_ttft_ms,
        ss && ss.client_ttft_ms,
        hasDec ? dec[`client_ttft_${k}_ms`] : null
      ));
      let sliceNote = (ds && ds.slice_note) || (ss && ss.slice_note) || null;
      let serverTtftClamped = false;
      let decodeFirstStep = null;
      let prefillEngineOverhead = null;
      let decodeHandoffOverhead = null;

      if (ds || ss) {
        prefill = nonnegFinite(firstFinite(ds && ds.prefill_ms, ds && ds.vllm_prefill_ms, ds && ds.prefill_time_ms, ss && ss.prefill_time_ms));
        queue = nonnegFinite(firstFinite(ds && ds.queue_ms, ds && ds.vllm_queue_ms, ds && ds.queue_time_ms, ss && ss.queue_time_ms));
        kvTransfer = nonnegFinite(firstFinite(ds && ds.kv_transfer_ms, ss && ss.kv_transfer_ms));
        epp = nonnegFinite(firstFinite(ds && ds.epp_scheduling_ms, ds && ds.epp_scheduler_ms, ss && ss.epp_scheduling_ms));
        const dsHasEng = Boolean(ds && Object.prototype.hasOwnProperty.call(ds, "engine_overhead_ms"));
        engOv = dsHasEng ? nonnegFinite(ds.engine_overhead_ms) : nonnegFinite(ss && ss.engine_overhead_ms);
        const dsHasGw = Boolean(
          ds && (
            Object.prototype.hasOwnProperty.call(ds, "gateway_overhead_ms")
            || Object.prototype.hasOwnProperty.call(ds, "overhead_ms")
            || Object.prototype.hasOwnProperty.call(ds, "client_gateway_epp_ms")
          )
        );
        gwOv = dsHasGw
          ? nonnegFinite(firstFinite(ds.gateway_overhead_ms, ds.overhead_ms, ds.client_gateway_epp_ms))
          : nonnegFinite(ss && ss.gateway_overhead_ms);
        ttftUnattr = nonnegFinite(firstFinite(
          ds && ds.ttft_unattributed_ms,
          ss && ss.ttft_unattributed_ms,
          (source === "pd_pooled_fallback" || (ss && ss.source === "client_fallback")) && ss
            ? firstFinite(ss.ttft_stage_ms, ss.client_ttft_ms)
            : null
        ));
        if (!sliceNote && source !== "pd_pooled_fallback" && ss && ss.source === "client_fallback" && k === "mean" && isFiniteMetric(ttftUnattr)) {
          sliceNote = "Server mean stage histograms were not recorded; showing exact client Mean TTFT.";
        }
        serverTtftClamped = Boolean((ds && ds.server_ttft_clamped !== undefined ? ds.server_ttft_clamped : (ss && ss.server_ttft_clamped)) || false);
        decodeFirstStep = nonnegFinite(firstFinite(ds && ds.decode_first_step_ms, ss && ss.decode_first_step_ms));
        prefillEngineOverhead = nonnegFinite(firstFinite(ds && ds.prefill_engine_overhead_ms, ss && ss.prefill_engine_overhead_ms));
        decodeHandoffOverhead = nonnegFinite(firstFinite(ds && ds.decode_handoff_overhead_ms, ss && ss.decode_handoff_overhead_ms));
      } else {
        prefill = nonnegFinite(dec[`vllm_prefill_${k}_ms`]);
        queue = nonnegFinite(dec[`vllm_queue_${k}_ms`]);
        kvTransfer = nonnegFinite(dec[`kv_transfer_${k}_ms`]);
        epp = nonnegFinite(dec[`epp_scheduler_${k}_ms`]);
        engOv = nonnegFinite(dec[`engine_overhead_${k}_ms`]);
        const flatOverhead = nonnegFinite(dec[`overhead_${k}_ms`]);
        gwOv = flatOverhead !== null && epp !== null
          ? round3Offline(Math.max(0, flatOverhead - epp))
          : flatOverhead;
        ttftUnattr = nonnegFinite(dec[`ttft_unattributed_${k}_ms`]);
      }

      if (source !== "pd_pooled_fallback") {
        const hasExplicitPdSub = isFiniteMetric(decodeFirstStep)
          && isFiniteMetric(prefillEngineOverhead)
          && isFiniteMetric(decodeHandoffOverhead);
        const unboundedMaxTail = k === "max" && (serverTtftClamped || gwOv === null);
        if (!hasExplicitPdSub && isFiniteMetric(engOv) && !serverTtftClamped && !unboundedMaxTail && roleBreakdown && roleBreakdown.prefill && roleBreakdown.decode) {
          const pRole = roleBreakdown.prefill;
          const dRole = roleBreakdown.decode;
          const rbSuffix = k === "max" ? (pRole.ttft_max_ms !== undefined ? "max" : "p99") : k;
          const reconciled = reconcileOfflinePdEngineOverhead({
            engineOverheadMs: engOv,
            serverTtftClamped,
            pTtft: nonnegFinite(pRole[`ttft_${rbSuffix}_ms`]),
            pQueueEff: effectiveOfflineRoleQueue(pRole[`queue_${rbSuffix}_ms`], pRole.queue_mean_ms, k),
            pPrefill: nonnegFinite(pRole[`prefill_${rbSuffix}_ms`]),
            dTtft: nonnegFinite(dRole[`ttft_${rbSuffix}_ms`]),
            dQueueEff: effectiveOfflineRoleQueue(
              firstFinite(dRole[`queue_raw_${rbSuffix}_ms`], dRole[`queue_${rbSuffix}_ms`]),
              firstFinite(dRole.queue_raw_mean_ms, dRole.queue_mean_ms),
              k
            ),
            dPrefill: nonnegFinite(dRole[`prefill_${rbSuffix}_ms`]),
          });
          decodeFirstStep = reconciled.decode_first_step_ms;
          prefillEngineOverhead = reconciled.prefill_engine_overhead_ms;
          decodeHandoffOverhead = reconciled.decode_handoff_overhead_ms;
        }
      }

      const hasPdSubSplit = source !== "pd_pooled_fallback"
        && isFiniteMetric(decodeFirstStep)
        && isFiniteMetric(prefillEngineOverhead)
        && isFiniteMetric(decodeHandoffOverhead);
      const hasServerComponents = prefill !== null
        || queue !== null
        || kvTransfer !== null
        || engOv !== null
        || gwOv !== null;
      const clientUnattr = source !== "pd_pooled_fallback" && !hasServerComponents
        ? ttftUnattr
        : null;

      return {
        key: k,
        label: `TTFT ${k.toUpperCase()}`,
        prefill_ms: prefill,
        decode_first_step_ms: hasPdSubSplit ? decodeFirstStep : null,
        queue_ms: queue,
        kv_transfer_ms: kvTransfer,
        prefill_engine_overhead_ms: hasPdSubSplit ? prefillEngineOverhead : null,
        decode_handoff_overhead_ms: hasPdSubSplit ? decodeHandoffOverhead : null,
        engine_overhead_unsplit_ms: hasPdSubSplit ? null : (source === "pd_pooled_fallback" ? null : engOv),
        epp_scheduling_ms: epp,
        engine_overhead_ms: engOv,
        gateway_overhead_ms: gwOv,
        ttft_unattributed_ms: ttftUnattr,
        client_unattributed_ms: clientUnattr,
        client_ttft_ms: clientTtftMs,
        has_pd_sub_split: hasPdSubSplit,
        server_ttft_clamped: serverTtftClamped,
        caveat: (ds && ds.caveat) || (ss && ss.caveat) || null,
        slice_note: sliceNote,
      };
    };

    const slices = ["min", "p50", "mean", "p90", "p99", "max"].map(extract).filter((s) => {
      if (!s) return false;
      if (source === "pd_pooled_fallback") {
        return (s.ttft_unattributed_ms || 0) > 0 || (s.kv_transfer_ms || 0) > 0 || (s.epp_scheduling_ms || 0) > 0;
      }
      const res = (s.epp_scheduling_ms || 0) + (s.engine_overhead_ms || 0) + (s.gateway_overhead_ms || 0);
      return (s.prefill_ms || 0) > 0 || (s.queue_ms || 0) > 0 || (s.kv_transfer_ms || 0) > 0 || res > 0 || (s.client_unattributed_ms || 0) > 0;
    });
    const hasKvTransfer = slices.some((s) => isFiniteMetric(s.kv_transfer_ms) && s.kv_transfer_ms > 0);
    const hasEppBar = slices.some((s) => isFiniteMetric(s.epp_scheduling_ms) && s.epp_scheduling_ms > 0);
    const hasEngineResidual = slices.some((s) => isFiniteMetric(s.engine_overhead_ms) && s.engine_overhead_ms > 0);
    const residualLabel = hasEngineResidual ? "Server & Gateway Residual" : "Client / Gateway / Routing";
    const unsplitEngineLabel = slices.some((s) => !s.has_pd_sub_split && s.server_ttft_clamped)
      ? "Server TTFT Residual"
      : "Engine / API Overhead";

    const candidateSegDefs = source === "pd_pooled_fallback"
      ? [
          { key: "ttft_unattributed_ms", label: "P/D Engine TTFT (Queue + Prefill)", color: PALETTE[0], opacity: 1 },
          { key: "kv_transfer_ms", label: "KV Transfer (NIXL)", color: PALETTE[4], opacity: 1 },
          { key: "epp_scheduling_ms", label: "EPP Scheduling", color: PALETTE[3], opacity: 1 },
        ]
      : [
          { key: "prefill_ms", label: "Prefill Time", color: PALETTE[0], opacity: 1 },
          { key: "decode_first_step_ms", label: "Decode First-Step Compute", color: PALETTE[5], opacity: 1 },
          { key: "queue_ms", label: "Queue Time", color: PALETTE[2], opacity: 1 },
          { key: "kv_transfer_ms", label: "KV Transfer (NIXL)", color: PALETTE[4], opacity: 1 },
          { key: "prefill_engine_overhead_ms", label: "Prefill Engine / API Overhead", color: PALETTE[1], opacity: 1 },
          { key: "decode_handoff_overhead_ms", label: "Decode Handoff / SSE Overhead", color: PALETTE[6], opacity: 1 },
          { key: "engine_overhead_unsplit_ms", label: unsplitEngineLabel, color: PALETTE[1], opacity: 1 },
          { key: "epp_scheduling_ms", label: "EPP Scheduling", color: PALETTE[3], opacity: 1 },
          { key: "gateway_overhead_ms", label: "Client / Gateway / Network", color: PALETTE[3], opacity: 0.48 },
          { key: "client_unattributed_ms", label: "Unattributed TTFT", color: PALETTE[2], opacity: 0.38 },
        ];
    const nonZeroSegDefs = candidateSegDefs.filter((def) =>
      slices.some((s) => isFiniteMetric(s[def.key]) && Number(s[def.key]) > 0)
    );
    const segDefs = nonZeroSegDefs.length
      ? nonZeroSegDefs
      : candidateSegDefs.filter((def) => slices.some((s) => isFiniteMetric(s[def.key])));

    const caveat = (useStageFallback ? (slbView && slbView.caveat) : (dec.caveat || (slbView && slbView.caveat)))
      || (slices.find((s) => s.caveat) || {}).caveat
      || null;
    return { source, slices, hasKvTransfer, hasEppBar, hasEngineResidual, residualLabel, unsplitEngineLabel, segDefs, caveat, reason: dec.reason };
  }

  function renderBottlenecks(pageNode) {
    const d = state.data;
    const f = d.forensics || {};
    const cs = d.client_series || {};
    const st = d.server_telemetry || {};
    const srvPts = serverSeriesPoints(st, cs);
    const bIsDisagg = isDisaggregatedReport(st, d);
    const bRoleSeries = Array.isArray(st.role_series) ? st.role_series : [];
    const bRoleLookup = buildRoleSeriesLookup(bRoleSeries);
    const bCleanedPods = (Array.isArray(st.per_pod) ? st.per_pod : []).filter((p) => effectiveReportPodRole(p) !== "runner");
    const findings = f.findings || [];
    const ROLE_BADGE_MAP = {
      prefill: { label: "Prefill (P)", cls: "info" },
      decode: { label: "Decode (D)", cls: "violet" },
      both: { label: "P + D", cls: "" },
    };
    const findingsBody = findings.length
      ? el(
          "div",
          null,
          findings.map((item) => {
            const cls = item.severity === "critical" ? "crit" : item.severity === "warning" ? "warn" : "info";
            const metricLine = item.metric ? `${item.metric}${item.value !== undefined && item.value !== null ? `: ${item.value}` : ""}` : null;
            const roleKey = item.role ? String(item.role).trim().toLowerCase() : "";
            const roleSpec = roleKey ? ROLE_BADGE_MAP[roleKey] || null : null;
            const evidenceList = Array.isArray(item.evidence)
              ? item.evidence.filter((ev) => (typeof ev === "string" && ev.trim()) || (ev && typeof ev === "object"))
              : [];
            const hasEvidenceList = evidenceList.length > 0;
            const evidenceRow = hasEvidenceList
              ? el("div", { className: "finding-evidence-row" }, [
                  el("span", { className: "kpi-sub", textContent: "Evidence:" }),
                  ...evidenceList.map((ev) =>
                    el("span", {
                      className: "finding-evidence-chip",
                      textContent: typeof ev === "string" ? ev : `${ev.metric ?? "metric"}: ${ev.value ?? "N/A"}`,
                    })
                  ),
                ])
              : null;
            return el("div", { className: "finding" }, [
              el("div", { className: "title-row" }, [
                el("span", { className: `badge ${cls}`, textContent: String(item.severity || "info").toUpperCase() }),
                el("strong", { textContent: item.title || item.code || "Finding" }),
                item.category ? el("span", { className: "badge", textContent: item.category }) : null,
                roleSpec ? el("span", { className: `badge ${roleSpec.cls}`.trim(), textContent: roleSpec.label }) : null,
                !hasEvidenceList && metricLine ? el("span", { className: "badge", textContent: metricLine }) : null,
              ]),
              el("div", { textContent: item.description || item.message || (typeof item.evidence === "string" ? item.evidence : "") }),
              evidenceRow,
              item.recommendation ? el("div", { className: "kpi-sub", textContent: `Recommendation: ${item.recommendation}` }) : null,
            ]);
          })
        )
      : el("div", { className: "empty-note", textContent: "No bottleneck findings emitted." });
    pageNode.appendChild(makeSection("b-verdict", "Ranked Findings", "Prioritized root-cause findings and tuning actions", findingsBody));

    const tlHost = el("div", { className: "sat-timeline-host" });
    const tlControls = el("div", { className: "stage-controls" });
    const { warmupEnd, drainStart } = resolveRunPhaseBoundaries(d);
    pageNode.appendChild(
      makeSection(
        "b-timeline",
        "Per-Role Saturation Timeline",
        "Quantitative per-role running/waiting requests, KV cache usage, and saturation state breakdown",
        tlHost,
        tlControls
      )
    );
    ForensicsTimelineSVG(tlHost, f.timeline_buckets || [], { warmupEnd, drainStart, controlsHost: tlControls });

    const slbView = buildOfflineStageLatencyView(f, st, cs, srvPts);
    const isServerAvailable = Boolean(st && st.status !== "unavailable" && st.available !== false && (st.status === "available" || st.available === true));
    const canJumpServingLatency = slbView.source === "server_telemetry"
      && isServerAvailable
      && Boolean(srvPts.length || Object.keys(st.summary || {}).length);
    const stageControls = el("div", { className: "stage-controls" });
    const domE2eBadge = el("span", { className: "badge info", style: "display:none" });
    const domTtftBadge = el("span", { className: "badge", style: "display:none" });
    const peakShiftBadge = slbView.peakWindowShift && slbView.peakWindowShift.detected
      ? el("span", { className: "badge warn", textContent: "Peak Shift" })
      : null;
    stageControls.append(domE2eBadge, domTtftBadge);
    if (peakShiftBadge) stageControls.appendChild(peakShiftBadge);

    let stageQuantile = slbView.primarySlice === "p90" ? "p90" : "p50";
    let stageP50Btn = null;
    let stageP90Btn = null;
    const bStageHost = el("div", { className: "chart-box" });
    const stageNoticeBox = el("div");
    const shareHead = el("div", { className: "stage-share-head" }, [
      el("span", { textContent: "Stage Share Summary" }),
      helpBadge("Stage Share Summary"),
    ]);
    const shareGrid = el("div", { className: "stage-share-grid" });
    const stageBodyChildren = [];
    if (slbView.peakWindowShift && slbView.peakWindowShift.detected) {
      const pws = slbView.peakWindowShift;
      const shiftText = pws.summary
        || `Peak window${isFiniteMetric(pws.peak_t_seconds) ? ` at t=${Math.round(Number(pws.peak_t_seconds))}s` : ""} shifted dominant stage from ${stageDisplayLabel(pws.baseline_stage, slbView.source)} to ${stageDisplayLabel(pws.peak_stage, slbView.source)}.`;
      stageBodyChildren.push(el("div", { className: "stage-shift-note", textContent: shiftText }));
    }

    function updateStageSection() {
      const hasP50Slice = Boolean(slbView.slices.p50 && slbView.slices.p50.available);
      const hasP90Slice = Boolean(slbView.slices.p90 && slbView.slices.p90.available);
      const hasMeanSlice = Boolean(slbView.slices.mean && slbView.slices.mean.available);
      const useP90 = stageQuantile === "p90" && slbView.hasP90;
      if (stageP50Btn) stageP50Btn.classList.toggle("active", !useP90);
      if (stageP90Btn) stageP90Btn.classList.toggle("active", useP90);

      const activeSliceKey = useP90 && hasP90Slice
        ? "p90"
        : (hasP50Slice ? "p50" : (hasP90Slice ? "p90" : (hasMeanSlice ? "mean" : null)));
      const activeSlice = activeSliceKey ? slbView.slices[activeSliceKey] : null;
      const domE2e = activeSliceKey === "p90"
        ? (slbView.primarySlice === "p90" ? (slbView.dominantE2eStageP90 ?? slbView.dominantE2eStage) : slbView.dominantE2eStageP90)
        : slbView.dominantE2eStage;
      const domTtft = activeSliceKey === "p90"
        ? (slbView.primarySlice === "p90" ? (slbView.dominantTtftStageP90 ?? slbView.dominantTtftStage) : slbView.dominantTtftStageP90)
        : slbView.dominantTtftStage;

      if (domE2e) {
        const e2eShare = activeSlice && activeSlice.share_of_e2e_pct ? firstFinite(activeSlice.share_of_e2e_pct[domE2e]) : null;
        const isHotE2e = domE2e === "queue_time" || domE2e === "kv_transfer" || domE2e === "gateway_overhead" || domE2e === "engine_overhead";
        domE2eBadge.className = `badge ${isHotE2e ? "warn" : "info"}`;
        domE2eBadge.textContent = e2eShare !== null
          ? `Dominant E2E: ${stageDisplayLabel(domE2e, slbView.source)} · ${pct(e2eShare)}`
          : `Dominant E2E: ${stageDisplayLabel(domE2e, slbView.source)}`;
        domE2eBadge.style.display = "";
      } else {
        domE2eBadge.style.display = "none";
      }

      if (domTtft) {
        const ttftShare = activeSlice && activeSlice.share_of_ttft_pct ? firstFinite(activeSlice.share_of_ttft_pct[domTtft]) : null;
        const isHotTtft = domTtft === "queue_time" || domTtft === "kv_transfer" || domTtft === "gateway_overhead" || domTtft === "engine_overhead";
        domTtftBadge.className = `badge ${isHotTtft ? "warn" : ""}`.trim();
        domTtftBadge.textContent = ttftShare !== null
          ? `Dominant TTFT: ${stageDisplayLabel(domTtft, slbView.source)} · ${pct(ttftShare)}`
          : `Dominant TTFT: ${stageDisplayLabel(domTtft, slbView.source)}`;
        domTtftBadge.style.display = "";
      } else {
        domTtftBadge.style.display = "none";
      }

      const noticeNodes = [];
      const activeCaveat = (activeSlice && activeSlice.caveat) || slbView.caveat;
      if (activeCaveat) {
        noticeNodes.push(el("div", { className: "stage-info-note", textContent: activeCaveat }));
      }
      const rec = activeSlice && activeSlice.reconciliation;
      if (rec && isFiniteMetric(rec.e2e_gap_pct) && Math.abs(Number(rec.e2e_gap_pct)) >= 15) {
        const sign = Number(rec.e2e_gap_ms) >= 0 ? "+" : "";
        const pctSign = Number(rec.e2e_gap_pct) >= 0 ? "+" : "";
        noticeNodes.push(
          el("div", {
            className: "kpi-sub",
            style: "margin-bottom:8px",
            textContent: `E2E reconciliation: stage sum (${msFmt(rec.total_stage_ms)}) vs Client E2E (${msFmt(rec.client_e2e_ms)}), gap ${sign}${msFmt(rec.e2e_gap_ms)} (${pctSign}${pct(rec.e2e_gap_pct)})${rec.quantile_non_additive ? " — marginal quantiles (P50/P90) are non-additive across stages." : "."}`,
          })
        );
      }
      stageNoticeBox.replaceChildren(...noticeNodes);

      const pts = slbView.series || [];
      const layers = offlineStageAreaLayers(slbView.source, slbView.hasKvTransfer);
      const pickLayerVal = (pt, l) => {
        if (!pt) return null;
        const primary = useP90 ? pt[l.p90Key] : pt[l.key];
        if (isFiniteMetric(primary)) return primary;
        if (useP90 && l.fallbackP90Key && isFiniteMetric(pt[l.fallbackP90Key])) return pt[l.fallbackP90Key];
        if (!useP90 && l.fallbackKey && isFiniteMetric(pt[l.fallbackKey])) return pt[l.fallbackKey];
        return primary;
      };
      const hasSeries = pts.some((pt) => layers.some((l) => isFiniteMetric(pickLayerVal(pt, l))));
      StackedAreaChart(bStageHost, {
        emptyText: slbView.source === "client_fallback"
          ? "Client-side stage latency time series unavailable."
          : `Stage latency time series (${useP90 ? "P90" : "P50"}) unavailable.`,
        yUnit: "ms",
        yFormat: msFmt,
        series: hasSeries
          ? layers.map((l) => ({
              label: l.label,
              color: l.color,
              ...(l.dashed ? { dashed: true } : {}),
              points: pts.map((pt) => ({ x: pt.t, y: pickLayerVal(pt, l) })),
            }))
          : [],
      });

      const cards = [];
      if (activeSlice && activeSlice.available) {
        if (slbView.source === "server_telemetry") {
          const specs = [
            { key: "epp_scheduling", label: "EPP Scheduling", ms: activeSlice.epp_scheduling_ms, isTtft: true },
            { key: "queue_time", label: "Queue Time", ms: activeSlice.queue_time_ms, isTtft: true },
            { key: "kv_transfer", label: "KV Transfer (NIXL)", ms: activeSlice.kv_transfer_ms, isTtft: true },
            { key: "prefill_time", label: "Prefill Time", ms: activeSlice.prefill_time_ms, isTtft: true },
            { key: "engine_overhead", label: "Server TTFT Residual", ms: activeSlice.engine_overhead_ms, isTtft: true },
            { key: "gateway_overhead", label: "Client / Gateway / Network", ms: activeSlice.gateway_overhead_ms, isTtft: true },
            { key: "decode_time", label: "Decode Time", ms: activeSlice.decode_time_ms, isTtft: false },
          ];
          for (const spec of specs) {
            if (!isFiniteMetric(spec.ms)) continue;
            const e2ePct = activeSlice.share_of_e2e_pct ? firstFinite(activeSlice.share_of_e2e_pct[spec.key]) : null;
            const ttftPct = spec.isTtft && activeSlice.share_of_ttft_pct ? firstFinite(activeSlice.share_of_ttft_pct[spec.key]) : null;
            const metaParts = [];
            if (e2ePct !== null) metaParts.push(`${pct(e2ePct)} of E2E`);
            if (ttftPct !== null) metaParts.push(`${pct(ttftPct)} of TTFT`);
            cards.push(
              el("div", { className: "stage-share-card" }, [
                el("div", { className: "kpi-label" }, [
                  el("span", { textContent: spec.label }),
                  helpBadge(spec.label),
                ]),
                el("div", { className: "stage-share-val", textContent: msFmt(spec.ms) }),
                el("div", { className: "stage-share-meta", textContent: metaParts.join(" · ") || `${activeSliceKey.toUpperCase()} slice` }),
              ])
            );
          }
        } else if (slbView.source === "pd_pooled_fallback") {
          const pdSpecs = [
            { key: "epp_scheduling", label: "EPP Scheduling", ms: activeSlice.epp_scheduling_ms, isTtft: true },
            { key: "kv_transfer", label: "KV Transfer (NIXL)", ms: activeSlice.kv_transfer_ms, isTtft: true },
            { key: "ttft_unattributed", label: "P/D Engine TTFT (Queue + Prefill)", ms: activeSlice.ttft_unattributed_ms ?? activeSlice.ttft_stage_ms, isTtft: true },
            { key: "decode_time", label: "Decode Time (Est.)", ms: activeSlice.decode_time_ms, isTtft: false },
          ];
          for (const spec of pdSpecs) {
            if (!isFiniteMetric(spec.ms)) continue;
            const e2ePct = activeSlice.share_of_e2e_pct ? firstFinite(activeSlice.share_of_e2e_pct[spec.key]) : null;
            const ttftPct = spec.isTtft && activeSlice.share_of_ttft_pct ? firstFinite(activeSlice.share_of_ttft_pct[spec.key]) : null;
            const metaParts = [];
            if (e2ePct !== null) metaParts.push(`${pct(e2ePct)} of E2E`);
            if (ttftPct !== null) metaParts.push(`${pct(ttftPct)} of TTFT`);
            cards.push(
              el("div", { className: "stage-share-card" }, [
                el("div", { className: "kpi-label" }, [
                  el("span", { textContent: spec.label }),
                  helpBadge(spec.label),
                ]),
                el("div", { className: "stage-share-val", textContent: msFmt(spec.ms) }),
                el("div", { className: "stage-share-meta", textContent: metaParts.join(" · ") || `${activeSliceKey.toUpperCase()} slice` }),
              ])
            );
          }
        } else {
          const clientSpecs = [
            { key: "ttft_unattributed", label: "Client TTFT (Unattributed)", ms: activeSlice.ttft_stage_ms },
            { key: "decode_time", label: "Decode Time (Est.)", ms: activeSlice.decode_time_ms },
          ];
          for (const spec of clientSpecs) {
            if (!isFiniteMetric(spec.ms)) continue;
            const e2ePct = activeSlice.share_of_e2e_pct ? firstFinite(activeSlice.share_of_e2e_pct[spec.key]) : null;
            cards.push(
              el("div", { className: "stage-share-card" }, [
                el("div", { className: "kpi-label" }, [
                  el("span", { textContent: spec.label }),
                  helpBadge(spec.label),
                ]),
                el("div", { className: "stage-share-val", textContent: msFmt(spec.ms) }),
                el("div", { className: "stage-share-meta", textContent: e2ePct !== null ? `${pct(e2ePct)} of E2E` : `${activeSliceKey.toUpperCase()} slice` }),
              ])
            );
          }
        }
      }
      shareGrid.replaceChildren(...cards);
    }

    if (slbView.available) {
      if (slbView.hasP90) {
        stageP50Btn = el("button", {
          type: "button",
          className: `pill-btn${stageQuantile === "p50" ? " active" : ""}`,
          textContent: "P50",
          onClick: () => {
            stageQuantile = "p50";
            updateStageSection();
          },
        });
        stageP90Btn = el("button", {
          type: "button",
          className: `pill-btn${stageQuantile === "p90" ? " active" : ""}`,
          textContent: "P90",
          onClick: () => {
            stageQuantile = "p90";
            updateStageSection();
          },
        });
        stageControls.append(stageP50Btn, stageP90Btn);
      }
      if (canJumpServingLatency) {
        stageControls.appendChild(
          el("button", {
            type: "button",
            className: "pill-btn",
            textContent: "Serving · Latency ->",
            onClick: () => {
              switchPage("serving");
              const targetEl = document.getElementById("s-latency");
              if (targetEl && typeof targetEl.scrollIntoView === "function") {
                targetEl.scrollIntoView({ behavior: "smooth" });
              }
            },
          })
        );
      }
      stageBodyChildren.push(stageNoticeBox, bStageHost, shareHead, shareGrid);
      if (slbView.source === "client_fallback" && !slbView.caveat) {
        stageBodyChildren.push(
          el("div", { className: "kpi-sub", style: "margin-top:8px", textContent: "Engine stage histograms unavailable — showing client TTFT vs estimated decode split." })
        );
      }
    } else {
      stageBodyChildren.push(
        el("div", { className: "empty-note", textContent: "Request stage latency breakdown unavailable (requires server stage histograms or client TTFT/E2E telemetry)." })
      );
    }

    const stageSub = slbView.source === "client_fallback"
      ? "Client TTFT vs estimated decode split over time (server stage histograms unavailable)"
      : slbView.source === "pd_pooled_fallback"
        ? "Client E2E/TTFT split with measured EPP Scheduling and NIXL KV Transfer"
        : "Stacked stage latencies over elapsed time with E2E and TTFT share attribution";
    pageNode.appendChild(makeSection("b-stage-breakdown", "Request Stage Latency Breakdown", stageSub, el("div", null, stageBodyChildren), stageControls));
    if (slbView.available) {
      updateStageSection();
    }

    const decompHost = el("div", { className: "chart-box" });
    const decompView = buildOfflineTtftDecomposition(f, slbView);
    const decItems = decompView.slices.map((s) => {
      const segs = (decompView.segDefs || []).map((def) => ({
        label: def.label,
        value: isFiniteMetric(s[def.key]) ? Number(s[def.key]) : 0,
        color: def.color,
        opacity: def.opacity,
      }));
      const total = segs.reduce((acc, item) => acc + Number(item.value || 0), 0);
      const posSegs = segs.filter((seg) => seg.value > 0);
      const tipSegs = posSegs.length ? posSegs : segs;
      const tipNotes = [
        isFiniteMetric(s.client_ttft_ms) ? `Client TTFT: ${msFmt(s.client_ttft_ms)}` : null,
        s.slice_note || s.caveat || null,
      ].filter(Boolean);
      return {
        label: s.label,
        tooltipTitle: s.label,
        tooltipItems: tipSegs.map((seg) => ({ label: seg.label, color: seg.color, opacity: seg.opacity, valueText: msFmt(seg.value) })),
        tooltipTotal: msFmt(total),
        tooltipNote: tipNotes,
        segments: segs,
      };
    });
    const decompChildren = [];
    if (decompView.caveat) {
      decompChildren.push(el("div", { className: "stage-info-note", textContent: decompView.caveat }));
    }
    decompChildren.push(decompHost);
    if (decompView.slices.length) {
      const p0 = ["p50", "mean", "p90", "p99", "min", "max"]
        .map((k) => decompView.slices.find((s) => s.key === k))
        .find(Boolean) || decompView.slices[0];
      const unsplitLabel = p0.server_ttft_clamped ? "Server TTFT Residual" : "Engine / API Overhead";
      const candidatePills = decompView.source === "pd_pooled_fallback"
        ? [
            { label: "P/D Engine TTFT (Queue + Prefill)", val: p0.ttft_unattributed_ms },
            ...(isFiniteMetric(p0.kv_transfer_ms) ? [{ label: "KV Transfer (NIXL)", val: p0.kv_transfer_ms }] : []),
            ...(isFiniteMetric(p0.epp_scheduling_ms) ? [{ label: "EPP Scheduling", val: p0.epp_scheduling_ms }] : []),
          ]
        : [
            { label: "Prefill Time", val: p0.prefill_ms },
            ...(p0.has_pd_sub_split ? [{ label: "Decode First-Step Compute", val: p0.decode_first_step_ms }] : []),
            { label: "Queue Time", val: p0.queue_ms },
            ...(isFiniteMetric(p0.kv_transfer_ms) ? [{ label: "KV Transfer (NIXL)", val: p0.kv_transfer_ms }] : []),
            ...(p0.has_pd_sub_split
              ? [
                  { label: "Prefill Engine / API Overhead", val: p0.prefill_engine_overhead_ms },
                  { label: "Decode Handoff / SSE Overhead", val: p0.decode_handoff_overhead_ms },
                ]
              : (isFiniteMetric(p0.engine_overhead_unsplit_ms)
                  ? [{ label: unsplitLabel, val: p0.engine_overhead_unsplit_ms }]
                  : [])),
            ...(isFiniteMetric(p0.epp_scheduling_ms) ? [{ label: "EPP Scheduling", val: p0.epp_scheduling_ms }] : []),
            ...(isFiniteMetric(p0.gateway_overhead_ms) ? [{ label: "Client / Gateway / Network", val: p0.gateway_overhead_ms }] : []),
            ...(isFiniteMetric(p0.client_unattributed_ms) ? [{ label: "Unattributed TTFT", val: p0.client_unattributed_ms }] : []),
          ];
      const finitePills = candidatePills.filter((item) => isFiniteMetric(item.val));
      const positivePills = finitePills.filter((item) => Number(item.val) > 0);
      const pillSpecs = positivePills.length ? positivePills : finitePills;
      if (pillSpecs.length) {
        decompChildren.push(
          el("div", { className: "stage-share-grid", style: "margin-top:10px" }, pillSpecs.map((item) =>
            el("div", { className: "stage-share-card" }, [
              el("div", { className: "kpi-label" }, [
                el("span", { textContent: item.label }),
                helpBadge(item.label),
              ]),
              el("div", { className: "stage-share-val", textContent: msFmt(item.val) }),
            ])
          ))
        );
      }
    }
    const decompSub = decompView.source === "pd_pooled_fallback"
      ? "P/D Engine TTFT + NIXL KV Transfer + EPP Scheduling"
      : (decompView.segDefs && decompView.segDefs.length
          ? decompView.segDefs.map((d) => d.label).join(" + ")
          : "Prefill, Queue, optional KV Transfer (NIXL), and engine/routing/network layers of observed client TTFT");
    pageNode.appendChild(makeSection("b-ttft-decomp", "TTFT Stage Decomposition", decompSub, el("div", null, decompChildren)));
    StackedBarAreaChart(decompHost, { grouped: false, emptyText: decompView.reason || "TTFT stage breakdown requires serving engine stage histograms.", items: decItems });

    const turnsHost = el("div", { className: "chart-box" });
    const rvc = f.routing_vs_cache || {};
    const stSummary = st.summary || {};
    const stSbr = st.summary_by_role || {};
    const stDerived = st.derived || {};
    const stRoleSum = stDerived.role_summary || {};
    const stMetricSources = st.metric_sources || {};
    const bIsPdCanonical = stMetricSources.pd_canonical_metrics === "role_separated";
    const bDurationSec = isFiniteMetric(cs.duration_seconds)
      ? Number(cs.duration_seconds)
      : (isFiniteMetric(cs.windows && cs.windows.full && cs.windows.full.duration_seconds)
          ? Number(cs.windows.full.duration_seconds)
          : (srvPts.length > 1 ? Math.max(0, srvPts[srvPts.length - 1]._t - srvPts[0]._t) : 0));
    const bStepSec = Number(st.step_seconds || 5);
    const bRoleSources = deriveRoleTokenSourcesData(st, cs, srvPts, bRoleSeries, stSummary, stDerived, bCleanedPods, bDurationSec, bStepSec, bIsDisagg);
    const { prefillAvoidedPct: bPrefillAvoidedPct, prefillLocalCacheShare: bPrefillLocalCacheShare } = resolveRoleSourceMetrics(bRoleSources);

    const sessionsSummary = d.sessions_summary || {};
    const turnCacheStats = (f.cache_behavior && f.cache_behavior.follow_up_turns)
      || sessionsSummary.followup_turn_stats
      || sessionsSummary.follow_up_turns
      || sessionsSummary.turn_cache_summary
      || null;

    let bCachedTps;
    let bFreshTps;
    if (bIsDisagg) {
      const pSbr = stSbr.prefill || {};
      const pDrv = stRoleSum.prefill || {};
      const rawPoolLocalCache = podStatMean(stSummary, "engine_prompt_local_cache_tps");
      const pLocalCache = podStatMean(pSbr, "engine_prompt_local_cache_tps")
        ?? firstFinite(pDrv.engine_prompt_local_cache_tps)
        ?? meanOfRoleSeries(bRoleSeries, "prefill_engine_prompt_local_cache_tps")
        ?? (rvc.role_scope === "prefill" ? firstFinite(rvc.engine_cached_tok_s) : null)
        ?? (rawPoolLocalCache !== null ? (bIsPdCanonical ? rawPoolLocalCache : rawPoolLocalCache * bPrefillLocalCacheShare) : null)
        ?? firstFinite(f.cache_behavior && f.cache_behavior.cached_tps)
        ?? 0;
      const pExtKv = podStatMean(pSbr, "engine_prompt_external_tps")
        ?? firstFinite(pDrv.engine_prompt_external_tps)
        ?? meanOfRoleSeries(bRoleSeries, "prefill_engine_prompt_external_tps")
        ?? 0;
      const pFresh = podStatMean(pSbr, "engine_prompt_local_compute_tps")
        ?? firstFinite(pDrv.engine_prompt_local_compute_tps)
        ?? meanOfRoleSeries(bRoleSeries, "prefill_engine_prompt_local_compute_tps")
        ?? firstFinite(rvc.engine_fresh_tok_s, podStatMean(stSummary, "engine_prompt_local_compute_tps"), f.cache_behavior && f.cache_behavior.fresh_tps)
        ?? 0;
      const pRecomp = podStatMean(pSbr, "engine_prompt_recomputed_tps")
        ?? firstFinite(pDrv.engine_prompt_recomputed_tps)
        ?? meanOfRoleSeries(bRoleSeries, "prefill_engine_prompt_recomputed_tps")
        ?? firstFinite(rvc.engine_recomputed_tok_s, podStatMean(stSummary, "engine_prompt_recomputed_tps"))
        ?? 0;
      bCachedTps = Math.max(0, pLocalCache) + Math.max(0, pExtKv);
      bFreshTps = Math.max(0, pFresh) + Math.max(0, pRecomp);
    } else {
      bCachedTps = Math.max(0, firstFinite(rvc.engine_cached_tok_s, podStatMean(stSummary, "engine_prompt_local_cache_tps"), f.cache_behavior && f.cache_behavior.cached_tps) ?? 0)
        + Math.max(0, firstFinite(podStatMean(stSummary, "engine_prompt_external_tps"), f.cache_behavior && f.cache_behavior.external_cached_tps) ?? 0);
      bFreshTps = Math.max(0, firstFinite(rvc.engine_fresh_tok_s, podStatMean(stSummary, "engine_prompt_local_compute_tps"), f.cache_behavior && f.cache_behavior.fresh_tps, stDerived.recomputed_token_rate_tps) ?? 0)
        + Math.max(0, firstFinite(rvc.engine_recomputed_tok_s) ?? 0);
    }

    const hasPromptBreakdown = bCachedTps > 0 || bFreshTps > 0;
    const tokenItems = hasPromptBreakdown
      ? [
          {
            label: "Prompt Token Throughput",
            tooltipTitle: "Prompt Token Throughput",
            tooltipItems: [
              { label: "Prefix Cache", color: PALETTE[0], valueText: `${num(bCachedTps, 1)} tok/s` },
              { label: "Prefill Compute", color: PALETTE[2], valueText: `${num(bFreshTps, 1)} tok/s` },
            ],
            tooltipTotal: `${num(bCachedTps + bFreshTps, 1)} tok/s`,
            segments: [
              { label: "Prefix Cache", value: bCachedTps, color: PALETTE[0] },
              { label: "Prefill Compute", value: bFreshTps, color: PALETTE[2] },
            ],
          },
        ]
      : [];
    const emptyTurnMsg = rvc.turn_cache_note || "Per-turn follow-up breakdown requires a session-bearing trace (e.g. BailianTrace).";
    const turnAnalysisCard = sessionsSummary.available && turnCacheStats
      ? el("div", { className: "turn-analysis-card" }, [
          el("div", { className: "turn-analysis-title", textContent: "Multi-Turn Follow-up Analysis (turn > 1)" }),
          renderStatStrip([
            { label: "Follow-up turns", value: intFmt(turnCacheStats.followup_count ?? turnCacheStats.count) },
            { label: "Follow-up regression rate", value: pct(turnCacheStats.followup_regression_pct ?? turnCacheStats.miss_rate_pct) },
            turnCacheStats.first_turn_ttft_p50_ms != null
              ? { label: "Turn 1 TTFT P50", value: msFmt(turnCacheStats.first_turn_ttft_p50_ms) }
              : null,
            (turnCacheStats.followup_ttft_p50_ms ?? turnCacheStats.p90_ttft_ms) != null
              ? { label: "Follow-up TTFT P50", value: msFmt(turnCacheStats.followup_ttft_p50_ms ?? turnCacheStats.p90_ttft_ms) }
              : null,
          ]),
        ])
      : el("div", { className: "empty-note", style: "margin-top:8px", textContent: emptyTurnMsg });
    const cacheSection = makeSection(
      "b-cache-turns",
      "Multi-Turn Prefix Cache Reuse",
      "Prompt token throughput (Prefix Cache vs Prefill Compute) and multi-turn cache misses",
      el("div", null, [turnsHost, turnAnalysisCard])
    );
    StackedBarAreaChart(turnsHost, { grouped: false, yLabel: "tok/s", emptyText: emptyTurnMsg, items: tokenItems });

    const rHost = el("div", { className: "chart-box" });
    const rSeries = Array.isArray(rvc.series) && rvc.series.length
      ? rvc.series
      : srvPts
          .map((p, idx) => {
            const rp = bRoleLookup.at(p, idx);
            const eppHit = p.router_input_token_rate_tps > 0 && Number.isFinite(p.router_cached_token_rate_tps)
              ? Math.min(100, Math.max(0, (p.router_cached_token_rate_tps / p.router_input_token_rate_tps) * 100))
              : null;
            const vllmHit = bIsDisagg ? firstFinite(rp.prefill_prefix_cache_hit_percent, hitPctFromPoint(p)) : hitPctFromPoint(p);
            return {
              t: p._t,
              epp_hit_pct: eppHit,
              vllm_hit_pct: vllmHit,
            };
          })
          .filter((pt) => pt.epp_hit_pct !== null || pt.vllm_hit_pct !== null);
    const bEppExpectedHit = firstFinite(rvc.epp_expected_hit_pct, stDerived.avoided_prefill_ratio);
    const bVllmActualHit = (bIsDisagg && rvc.role_scope !== "prefill")
      ? (podStatMean(stSbr.prefill, "prefix_cache_hit_percent")
          ?? (bIsPdCanonical ? hitPctFromSummary(stSummary) : bPrefillAvoidedPct)
          ?? firstFinite(stRoleSum.prefill && stRoleSum.prefill.prefix_cache_hit_percent)
          ?? firstFinite(rvc.vllm_actual_hit_pct))
      : firstFinite(rvc.vllm_actual_hit_pct);
    const bGapPct = (bIsDisagg && rvc.role_scope !== "prefill" && bEppExpectedHit !== null && bVllmActualHit !== null)
      ? (bEppExpectedHit - bVllmActualHit)
      : firstFinite(rvc.gap_pct);
    const rSub = bEppExpectedHit !== null || bVllmActualHit !== null
      ? `EPP predicted: ${pct(bEppExpectedHit)} | vLLM actual: ${pct(bVllmActualHit)} | Prediction gap: ${pct(bGapPct)}`
      : "EPP predicted prefix cache hit rate (%) vs vLLM actual prefix cache hit rate (%)";
    const routingSection = makeSection("b-routing", "EPP Predicted vs vLLM Actual Prefix Cache Hit Rate", rSub, rHost);
    LineBandChart(rHost, {
      emptyText: "EPP vs vLLM prefix-cache comparison requires EPP and vLLM cache telemetry.",
      yMin: 0,
      yMax: 100,
      series: rSeries.length
        ? [
            { label: "EPP Predicted Prefix Cache Hit Rate (%)", points: rSeries.map((p) => ({ x: p.t, y: clampPct(p.epp_hit_pct) })) },
            { label: "vLLM Actual Prefix Cache Hit Rate (%)", points: rSeries.map((p) => ({ x: p.t, y: clampPct(p.vllm_hit_pct) })) },
          ]
        : [],
    });
    pageNode.appendChild(el("div", { className: "grid-2" }, [cacheSection, routingSection]));

    const skew = f.pod_skew || {};
    const pods = skew.pods || (st.per_pod || []).filter((p) => {
      const r = effectiveReportPodRole(p);
      return r !== "runner" && r !== "epp";
    });
    const skewBody = pods.length
      ? el("div", null, [
          el("div", { className: "kpi-sub th-inner", style: "margin-bottom:8px" }, [
            el("span", { textContent: `Load skew ratio (max / mean): ${num(skew.skew_ratio, 2)}x` }),
            helpBadge("Load skew ratio (max / mean)"),
          ]),
          makeSortableTable(
            [
              { key: "pod", label: "Pod" },
              { key: "role", label: "Role" },
              { key: "avg_running", label: "Running", num: true, getter: (r) => r.running_mean ?? r.avg_running ?? (r.running_requests && r.running_requests.mean), format: (v) => num(v, 1) },
              { key: "peak_waiting", label: "Waiting", num: true, getter: (r) => r.waiting_mean ?? r.peak_waiting ?? (r.waiting_requests && r.waiting_requests.max), format: (v) => num(v, 1) },
              { key: "peak_kv_cache_pct", label: "Peak KV Cache (%)", num: true, getter: (r) => r.kv_cache_peak_pct ?? r.peak_kv_cache_pct ?? (r.kv_cache_usage_percent && r.kv_cache_usage_percent.max), format: pct },
              { key: "prefix_hit_pct", label: "Prefix Cache Hit Rate (%)", num: true, getter: (r) => r.prefix_cache_hit_pct ?? r.prefix_hit_pct ?? hitPctFromSummary(r), format: pct },
            ],
            pods
          ),
        ])
      : el("div", { className: "empty-note", textContent: "Per-pod serving breakdown unavailable." });
    pageNode.appendChild(makeSection("b-pods", "Per-Pod Load Skew", "Pod utilization balance across pool", skewBody));
  }

  const RUNNER_POD_RE = /^(?:llm-d-lens-sim-|trace-replayer-|aiperf-|lens-eval-)/i;
  const EPP_POD_RE = /(?:^|[-_])(epp|router|gateway)(?:[-_]|$)/i;

  function isRunnerPod(pod) {
    if (!pod || typeof pod !== "object") return false;
    const role = String(pod.display_role || pod.role || "").toLowerCase();
    if (role === "client" || role === "benchmark" || role === "runner") return true;
    return RUNNER_POD_RE.test(String(pod.pod || pod.name || ""));
  }

  function effectiveReportPodRole(pod) {
    if (isRunnerPod(pod)) return "runner";
    const role = String(pod.display_role || pod.role || "").toLowerCase();
    if (role === "epp" || EPP_POD_RE.test(String(pod.pod || pod.name || ""))) return "epp";
    return role || "worker";
  }

  function podStatMean(pod, key) {
    const raw = pod && pod[key];
    if (typeof raw !== "boolean" && isFiniteMetric(raw)) return Number(raw);
    if (raw && typeof raw === "object" && typeof raw.mean !== "boolean" && isFiniteMetric(raw.mean)) return Number(raw.mean);
    return null;
  }

  function statMax(stat) {
    if (stat && typeof stat === "object" && typeof stat.max !== "boolean" && isFiniteMetric(stat.max)) {
      return Number(stat.max);
    }
    return null;
  }

  function statMin(stat) {
    if (stat && typeof stat === "object" && typeof stat.min !== "boolean" && isFiniteMetric(stat.min)) {
      return Number(stat.min);
    }
    return null;
  }

  function firstFinite(...vals) {
    for (const v of vals) {
      if (typeof v !== "boolean" && isFiniteMetric(v)) return Number(v);
    }
    return null;
  }

  function buildReportFlowStage(role, label, pods) {
    const names = pods.map((p) => p.pod || p.name).filter(Boolean);
    const sumField = (key) => {
      const vals = pods.map((p) => podStatMean(p, key)).filter((v) => v !== null);
      return vals.length ? vals.reduce((a, b) => a + b, 0) : null;
    };
    const avgField = (key) => {
      const vals = pods.map((p) => podStatMean(p, key)).filter((v) => v !== null);
      return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
    };
    return {
      role,
      label,
      pods: names,
      request_rate: sumField("request_rate_rps"),
      input_tok_s: sumField("input_token_rate_tps"),
      output_tok_s: sumField("output_token_rate_tps"),
      running: sumField("running_requests"),
      waiting: sumField("waiting_requests"),
      kv_pct: avgField("kv_cache_usage_percent"),
      prefix_hit_pct: avgField("prefix_cache_hit_percent"),
    };
  }

  function buildReportFlowStages(st) {
    const rawPerPod = Array.isArray(st && st.per_pod) ? st.per_pod : [];
    const servingPods = rawPerPod.filter((p) => effectiveReportPodRole(p) !== "runner");
    const prefill = servingPods.filter((p) => effectiveReportPodRole(p) === "prefill");
    const decode = servingPods.filter((p) => effectiveReportPodRole(p) === "decode");
    const workers = servingPods.filter((p) => {
      const r = effectiveReportPodRole(p);
      return r === "worker" || r === "model-server";
    });
    const eppPods = servingPods.filter((p) => effectiveReportPodRole(p) === "epp");
    const stages = [];
    const router = (st && st.router) || {};
    const summary = (st && st.summary) || {};
    const eppReq = podStatMean(router, "request_rate_rps");
    const eppInflight = podStatMean(summary, "epp_inflight_requests") ?? podStatMean(router, "inflight_requests");
    if (eppReq !== null || eppInflight !== null || eppPods.length > 0) {
      stages.push({
        role: "epp",
        label: "Endpoint Picker (EPP)",
        pods: eppPods.map((p) => p.pod || p.name).filter(Boolean),
        request_rate: eppReq,
        input_tok_s: null,
        output_tok_s: null,
        running: eppInflight,
        waiting: null,
        kv_pct: null,
        prefix_hit_pct: null,
      });
    }
    if (String((st && st.topology_mode) || "").toLowerCase() === "aggregated") {
      const agg = workers.length ? workers : (decode.length || prefill.length ? prefill.concat(decode) : servingPods.filter((p) => effectiveReportPodRole(p) !== "epp"));
      if (agg.length) stages.push(buildReportFlowStage("worker", "Workers", agg));
    } else {
      if (prefill.length) stages.push(buildReportFlowStage("prefill", "Prefill", prefill));
      if (decode.length) stages.push(buildReportFlowStage("decode", "Decode", decode));
      if (workers.length) stages.push(buildReportFlowStage("worker", "Workers", workers));
    }
    return stages;
  }

  const BINARY_POWER_STEP = 1024;
  const BYTE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  const BYTE_RATE_UNITS = ["B/s", "KiB/s", "MiB/s", "GiB/s", "TiB/s", "PiB/s"];

  function finiteBinaryMetric(v) {
    if (v === null || v === undefined || typeof v === "boolean" || Array.isArray(v)) return null;
    if (typeof v === "string" && v.trim() === "") return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  function scaleWithUnits(v, units) {
    const n = finiteBinaryMetric(v);
    if (n === null) return null;
    const abs = Math.abs(n);
    let unitIndex = 0;
    let divisor = 1;
    while (unitIndex < units.length - 1 && abs >= divisor * BINARY_POWER_STEP) {
      divisor *= BINARY_POWER_STEP;
      unitIndex += 1;
    }
    if (unitIndex < units.length - 1 && Math.round((abs / divisor) * 100) >= BINARY_POWER_STEP * 100) {
      divisor *= BINARY_POWER_STEP;
      unitIndex += 1;
    }
    const scaled = n / divisor;
    return {
      value: scaled === 0 ? 0 : scaled,
      unit: units[unitIndex],
      unitIndex,
      divisor,
    };
  }

  // eslint-disable-next-line no-unused-vars -- exported for unit tests in test_report_builder.py
  function scaleByteValue(v) {
    return scaleWithUnits(v, BYTE_UNITS);
  }

  function selectScaleWithUnits(values, units) {
    let maxAbs = 0;
    if (Array.isArray(values)) {
      for (const v of values) {
        const n = finiteBinaryMetric(v);
        if (n !== null) {
          const abs = Math.abs(n);
          if (abs > maxAbs) maxAbs = abs;
        }
      }
    }
    const scaled = scaleWithUnits(maxAbs, units);
    return {
      unit: scaled.unit,
      divisor: scaled.divisor,
      unitIndex: scaled.unitIndex,
    };
  }

  function selectByteScale(values) {
    return selectScaleWithUnits(values, BYTE_UNITS);
  }

  function selectByteRateScale(values) {
    return selectScaleWithUnits(values, BYTE_RATE_UNITS);
  }

  function formatWithUnits(v, units) {
    const scaled = scaleWithUnits(v, units);
    if (!scaled) return "N/A";
    return `${fmtDec(scaled.value)} ${scaled.unit}`;
  }

  function bytesFmt(v) {
    return formatWithUnits(v, BYTE_UNITS);
  }

  // eslint-disable-next-line no-unused-vars -- exported for unit tests in test_report_builder.py
  function byteRateFmt(bps) {
    return formatWithUnits(bps, BYTE_RATE_UNITS);
  }

  function formatBinaryPair(a, b, units) {
    const sa = scaleWithUnits(a, units);
    const sb = scaleWithUnits(b, units);
    if (!sa || !sb) return null;
    if (sa.value === 0 && sb.value === 0) return `${fmtDec(0)} / ${fmtDec(0)} ${units[0]}`;
    if (sa.value === 0) return `${fmtDec(0)} / ${fmtDec(sb.value)} ${sb.unit}`;
    if (sb.value === 0) return `${fmtDec(sa.value)} / ${fmtDec(0)} ${sa.unit}`;
    if (sa.unitIndex === sb.unitIndex) return `${fmtDec(sa.value)} / ${fmtDec(sb.value)} ${sa.unit}`;
    return `${fmtDec(sa.value)} ${sa.unit} / ${fmtDec(sb.value)} ${sb.unit}`;
  }

  function formatBytePair(a, b) {
    const paired = formatBinaryPair(a, b, BYTE_UNITS);
    if (paired !== null) return paired;
    const fa = finiteBinaryMetric(a);
    const fb = finiteBinaryMetric(b);
    if (fa === null && fb === null) return "N/A";
    return `${fa !== null ? bytesFmt(fa) : "N/A"} / ${fb !== null ? bytesFmt(fb) : "N/A"}`;
  }

  function formatByteRatePair(a, b) {
    const fa = finiteBinaryMetric(a);
    const fb = finiteBinaryMetric(b);
    if (fa === null && fb === null) return null;
    return formatBinaryPair(fa ?? 0, fb ?? 0, BYTE_RATE_UNITS);
  }

  function computeKvTierShareData(srvPts, cacheConfig, summary) {
    const cfg = cacheConfig && typeof cacheConfig === "object" ? cacheConfig : {};
    const engines = Array.isArray(cfg.engines) ? cfg.engines : [];
    const eng0 = engines[0] || {};
    const pts = Array.isArray(srvPts) ? srvPts : [];

    const hbmBytes = positiveFinite(cfg.hbm_capacity_bytes);
    const hostBytes = positiveFinite(cfg.cpu_capacity_bytes);
    const nvmeBytes = positiveFinite(cfg.nvme_capacity_bytes) ?? positiveFinite(cfg.disk_capacity_bytes);

    const hbmTokens = positiveFinite(cfg.hbm_capacity_tokens ?? eng0.kv_cache_size_tokens);
    const hostTokens = positiveFinite(cfg.cpu_capacity_tokens);
    const nvmeTokens = positiveFinite(cfg.nvme_capacity_tokens ?? cfg.disk_capacity_tokens);

    const hbmSampleVals = [];
    const hostSampleVals = [];
    const nvmeSampleVals = [];
    for (const p of pts) {
      const hu = isFiniteMetric(p.kv_cache_usage_percent)
        ? clampPct(p.kv_cache_usage_percent)
        : clampPct(p.gpu_cache_usage_percent);
      const cu = clampPct(p.cpu_cache_usage_percent);
      const du = clampPct(p.disk_cache_usage_percent);
      if (hu !== null) hbmSampleVals.push(hu);
      if (cu !== null) hostSampleVals.push(cu);
      if (du !== null) nvmeSampleVals.push(du);
    }

    const avg = (arr) => (arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : null);
    const hbmUsageMean = avg(hbmSampleVals) ?? podStatMean(summary, "kv_cache_usage_percent") ?? podStatMean(summary, "gpu_cache_usage_percent");
    const hostUsageMean = avg(hostSampleVals) ?? podStatMean(summary, "cpu_cache_usage_percent");
    const nvmeUsageMean = avg(nvmeSampleVals) ?? podStatMean(summary, "disk_cache_usage_percent");

    const hasHostPositive = hostSampleVals.some((v) => v > 0) || (positiveFinite(podStatMean(summary, "cpu_cache_usage_percent")) !== null);
    const hasNvmePositive = nvmeSampleVals.some((v) => v > 0) || (positiveFinite(podStatMean(summary, "disk_cache_usage_percent")) !== null);

    const hbmConfigured = Boolean(hbmBytes !== null || hbmTokens !== null || hbmSampleVals.length > 0 || hbmUsageMean !== null);
    const hostConfigured = Boolean(cfg.host_kv_configured || hostBytes !== null || hostTokens !== null || hasHostPositive);
    const nvmeConfigured = Boolean(cfg.nvme_kv_configured || nvmeBytes !== null || nvmeTokens !== null || hasNvmePositive);

    let idleCapWeights = { hbm: 1, host: 0, nvme: 0 };
    if (
      (hostConfigured || nvmeConfigured) &&
      hbmBytes !== null &&
      (!hostConfigured || hostBytes !== null) &&
      (!nvmeConfigured || nvmeBytes !== null)
    ) {
      const capHost = hostConfigured ? hostBytes : 0;
      const capNvme = nvmeConfigured ? nvmeBytes : 0;
      if (hbmBytes + capHost + capNvme > 0) {
        idleCapWeights = { hbm: hbmBytes, host: capHost, nvme: capNvme };
      }
    } else if (hostConfigured && !nvmeConfigured && hbmTokens !== null && hostTokens !== null) {
      if (hbmTokens + hostTokens > 0) {
        idleCapWeights = { hbm: hbmTokens, host: hostTokens, nvme: 0 };
      }
    }

    const tierPts = [];
    for (const p of pts) {
      const rawHbm = isFiniteMetric(p.kv_cache_usage_percent)
        ? clampPct(p.kv_cache_usage_percent)
        : clampPct(p.gpu_cache_usage_percent);
      const rawHost = clampPct(p.cpu_cache_usage_percent);
      const rawNvme = clampPct(p.disk_cache_usage_percent);
      if (rawHbm === null && rawHost === null && rawNvme === null) continue;

      const hbmU = rawHbm ?? 0;
      const hostU = rawHost ?? 0;
      const nvmeU = rawNvme ?? 0;
      const hostActive = hostU > 0;
      const nvmeActive = nvmeU > 0;

      let wHbm;
      let wHost;
      let wNvme;
      if (hbmBytes !== null && (!hostActive || hostBytes !== null) && (!nvmeActive || nvmeBytes !== null)) {
        wHbm = hbmU * hbmBytes;
        wHost = hostActive ? hostU * hostBytes : 0;
        wNvme = nvmeActive ? nvmeU * nvmeBytes : 0;
      } else if (hbmTokens !== null && (!hostActive || hostTokens !== null) && !nvmeActive) {
        wHbm = hbmU * hbmTokens;
        wHost = hostActive ? hostU * hostTokens : 0;
        wNvme = 0;
      } else {
        wHbm = hbmU;
        wHost = hostU;
        wNvme = nvmeU;
      }

      const denom = wHbm + wHost + wNvme;
      if (denom > 0) {
        tierPts.push({
          x: p._t,
          hbm_share_pct: (wHbm / denom) * 100,
          host_share_pct: (wHost / denom) * 100,
          nvme_share_pct: (wNvme / denom) * 100,
        });
      } else if (rawHbm !== null) {
        const idleDenom = idleCapWeights.hbm + idleCapWeights.host + idleCapWeights.nvme;
        tierPts.push({
          x: p._t,
          hbm_share_pct: (idleCapWeights.hbm / idleDenom) * 100,
          host_share_pct: (idleCapWeights.host / idleDenom) * 100,
          nvme_share_pct: (idleCapWeights.nvme / idleDenom) * 100,
        });
      }
    }

    return {
      points: tierPts,
      meanShares: {
        hbm: avg(tierPts.map((pt) => pt.hbm_share_pct)),
        host: avg(tierPts.map((pt) => pt.host_share_pct)),
        nvme: avg(tierPts.map((pt) => pt.nvme_share_pct)),
      },
      meanUsage: {
        hbm: hbmUsageMean,
        host: hostUsageMean,
        nvme: nvmeUsageMean,
      },
      configured: {
        hbm: hbmConfigured,
        host: hostConfigured,
        nvme: nvmeConfigured,
      },
    };
  }

  function renderServing(pageNode) {
    const d = state.data;
    const st = d.server_telemetry || {};
    const cs = d.client_series || {};
    const tMeta = d.task || {};
    const srvPts = serverSeriesPoints(st, cs);
    const roleSeries = Array.isArray(st.role_series) ? st.role_series : [];
    const summary = st.summary || {};
    const router = st.router || {};
    const derived = st.derived || {};
    const energy = st.energy || {};
    const cacheConfig = st.cache_config || st.cache_config_info || {};
    const cacheEngines = Array.isArray(cacheConfig.engines) ? cacheConfig.engines : [];
    const perEndpoint = Array.isArray(st.per_endpoint) ? st.per_endpoint : [];
    const deviceTelemetry = st.device_telemetry || {};
    const allocatedDevices = Array.isArray(deviceTelemetry.devices) ? deviceTelemetry.devices : [];
    const metricSources = st.metric_sources || {};
    const avail = st.status !== "unavailable" && srvPts.length > 0;
    const unavailableMsg = st.reason || "Server telemetry unavailable: endpoint is external or Prometheus is not configured.";
    const hwLabel = getHwLabel(st);
    const cleanedPods = (Array.isArray(st.per_pod) ? st.per_pod : []).filter((p) => effectiveReportPodRole(p) !== "runner");
    const rawTopoMode = resolveRawTopologyMode(st, d);
    const isDisaggTopo = rawTopoMode === "disaggregated";
    const isDisaggregated = isDisaggregatedReport(st, d);
    const isPdCanonical = metricSources.pd_canonical_metrics === "role_separated";
    const roleLookup = buildRoleSeriesLookup(roleSeries);
    const sbr = st.summary_by_role || {};
    const roleSum = derived.role_summary || {};
    const engDurationSec = isFiniteMetric(cs.duration_seconds)
      ? Number(cs.duration_seconds)
      : (isFiniteMetric(cs.windows && cs.windows.full && cs.windows.full.duration_seconds)
          ? Number(cs.windows.full.duration_seconds)
          : (isFiniteMetric(tMeta.duration_seconds)
              ? Number(tMeta.duration_seconds)
              : (srvPts.length > 1 ? Math.max(0, srvPts[srvPts.length - 1]._t - srvPts[0]._t) : 0)));
    const engStepSec = Number(st.step_seconds || 5);
    const roleSourcesData = deriveRoleTokenSourcesData(
      st,
      cs,
      srvPts,
      roleSeries,
      summary,
      derived,
      cleanedPods,
      engDurationSec,
      engStepSec,
      isDisaggregated
    );
    const roleSourceMetrics = resolveRoleSourceMetrics(roleSourcesData);
    const prefillLocalCacheShare = roleSourceMetrics.prefillLocalCacheShare;

    const prefillLocalCacheTps = (pt, idx) => {
      const rp = roleLookup.at(pt, idx);
      if (isDisaggregated) {
        if (isFiniteMetric(rp.prefill_engine_prompt_local_cache_tps)) return Number(rp.prefill_engine_prompt_local_cache_tps);
        if (isFiniteMetric(pt.engine_prompt_local_cache_tps)) {
          return isPdCanonical ? Number(pt.engine_prompt_local_cache_tps) : Number(pt.engine_prompt_local_cache_tps) * prefillLocalCacheShare;
        }
        return null;
      }
      return isFiniteMetric(pt.engine_prompt_local_cache_tps) ? Number(pt.engine_prompt_local_cache_tps) : null;
    };
    const prefillExtTps = (pt, idx) => {
      const rp = roleLookup.at(pt, idx);
      if (isDisaggregated) {
        if (isFiniteMetric(rp.prefill_engine_prompt_external_tps)) return Number(rp.prefill_engine_prompt_external_tps);
        const pRow = roleSourceMetrics.prefillRoleRow;
        if (pRow && Number(pRow.external_kv_tokens || 0) > 0 && Number(pRow.total_tokens || 0) > 0 && isFiniteMetric(pt.engine_prompt_external_tps)) {
          const dRow = roleSourceMetrics.decodeRoleRow;
          const pEk = Number(pRow.external_kv_tokens || 0);
          const dEk = Number((dRow && dRow.external_kv_tokens) || 0);
          const share = (pEk + dEk) > 0 ? pEk / (pEk + dEk) : 1;
          return Number(pt.engine_prompt_external_tps) * share;
        }
        const hasAnySrcMetric = isFiniteMetric(rp.prefill_engine_prompt_local_cache_tps)
          || isFiniteMetric(rp.prefill_engine_prompt_local_compute_tps)
          || isFiniteMetric(pt.engine_prompt_local_cache_tps)
          || isFiniteMetric(pt.engine_prompt_local_compute_tps)
          || isFiniteMetric(pt.engine_prompt_external_tps);
        return hasAnySrcMetric ? 0 : null;
      }
      return isFiniteMetric(pt.engine_prompt_external_tps) ? Number(pt.engine_prompt_external_tps) : null;
    };
    const prefillFreshTps = (pt, idx) => {
      const rp = roleLookup.at(pt, idx);
      if (isDisaggregated && isFiniteMetric(rp.prefill_engine_prompt_local_compute_tps)) {
        return Number(rp.prefill_engine_prompt_local_compute_tps);
      }
      return isFiniteMetric(pt.engine_prompt_local_compute_tps) ? Number(pt.engine_prompt_local_compute_tps) : null;
    };

    const kvMetrics = resolveEffectiveKvMetrics(st, srvPts, roleLookup, cleanedPods, isDisaggregated);
    const { decodeKvPoint, prefillKvPoint, effectiveKvPoint, hasDecodeKv, hasPrefillKv, kvPeak, kvMean } = kvMetrics;
    const prefixHitMetrics = resolveEffectivePrefixHitMetrics(st, roleSourcesData, isDisaggregated);

    const hasDisaggRoles = roleSeries.some((rp) => isFiniteMetric(rp.prefill_running_requests) || isFiniteMetric(rp.prefill_waiting_requests));
    const topoLabel = isDisaggregated
      ? "P/D Disaggregation"
      : (st.topology_mode || "aggregated");

    // 1. #s-flow · Topology & Traffic Replay
    const baseStages = buildReportFlowStages(st);
    const hasSummaryTraffic = baseStages.some((s) => isFiniteMetric(s.request_rate) || isFiniteMetric(s.output_tok_s))
      || isFiniteMetric(podStatMean(summary, "request_rate_rps"))
      || isFiniteMetric(podStatMean(summary, "output_token_rate_tps"));
    const engineStageCount = baseStages.filter((s) => s.role !== "epp").length;
    const flowFrames = srvPts.map((pt, idx) => {
      const rp = roleLookup.at(pt, idx);
      const stageRows = baseStages.map((s) => {
        if (s.role === "epp") {
          return {
            ...s,
            request_rate: isFiniteMetric(pt.router_request_rate_rps) ? pt.router_request_rate_rps : (isFiniteMetric(pt.request_rate_rps) ? pt.request_rate_rps : null),
            running: isFiniteMetric(pt.epp_inflight_requests) ? pt.epp_inflight_requests : (isFiniteMetric(pt.router_running_requests) ? pt.router_running_requests : null),
          };
        }
        const sole = engineStageCount === 1;
        const pick = (suffix, fallbackVal) => {
          const k = `${s.role}_${suffix}`;
          return isFiniteMetric(rp[k]) ? Number(rp[k]) : (sole && isFiniteMetric(fallbackVal) ? Number(fallbackVal) : null);
        };
        return {
          ...s,
          request_rate: pick("request_rate_rps", pt.request_rate_rps),
          input_tok_s: pick("input_token_rate_tps", pt.input_token_rate_tps),
          output_tok_s: pick("output_token_rate_tps", pt.output_token_rate_tps),
          running: pick("running_requests", pt.running_requests),
          waiting: pick("waiting_requests", pt.waiting_requests ?? pt.queue_depth),
          kv_pct: pick("kv_cache_usage_percent", pt.kv_cache_usage_percent),
          prefix_hit_pct: pick("prefix_cache_hit_percent", hitPctFromPoint(pt)),
        };
      });
      const active = stageRows.some((s) => (s.request_rate || 0) > 0 || (s.output_tok_s || 0) > 0 || (s.running || 0) > 0);
      return { t: pt._t, stages: stageRows, active };
    });

    let flowBody = null;

    if (avail && baseStages.length > 0 && (hasSummaryTraffic || flowFrames.some((f) => f.active))) {
      const flowWrap = el("div");
      const firstActive = flowFrames.findIndex((f) => f.active);
      let replayMode = flowFrames.length > 1 ? "replay" : "summary";
      let frameIdx = firstActive >= 0 ? firstActive : 0;
      let playing = false;
      let timer = null;
      const ctrlBar = el("div", { className: "filter-bar" });
      const cardsGrid = el("div", { className: "grid-kpis" });
      const scrubberRow = el("div", { className: "filter-bar" });

      function drawFlowStageCards() {
        const currentStages = replayMode === "replay" && flowFrames[frameIdx] ? flowFrames[frameIdx].stages : baseStages;
        const nodes = currentStages.map((s) =>
          el("div", { className: "kpi" }, [
            el("div", { className: "kpi-label" }, [
              el("span", { textContent: `${s.label}${s.pods && s.pods.length ? ` (${s.pods.length})` : ""}` }),
              helpBadge(s.label),
            ]),
            el("div", { className: "kpi-val", textContent: isFiniteMetric(s.request_rate) ? `${num(s.request_rate, 2)} req/s` : (isFiniteMetric(s.running) ? `${num(s.running, 1)} req` : "N/A") }),
            el("div", {
              className: "kpi-sub",
              textContent: s.role === "epp"
                ? `In-flight: ${num(s.running, 1)}`
                : `Prefill / Decode: ${num(s.input_tok_s, 0)} / ${num(s.output_tok_s, 0)} tok/s | Run: ${num(s.running, 1)} | Wait: ${num(s.waiting, 1)} | KV: ${pct(s.kv_pct)}`,
            }),
          ])
        );
        cardsGrid.replaceChildren(...nodes);
      }

      if (flowFrames.length > 1) {
        const modeReplayBtn = el("button", { type: "button", className: "pill-btn active", textContent: "Replay" });
        const modeSummaryBtn = el("button", { type: "button", className: "pill-btn", textContent: "Summary" });
        const playBtn = el("button", { type: "button", className: "pill-btn", textContent: "Play" });
        const timeLabel = el("span", { className: "kpi-sub mono" });
        const slider = el("input", { type: "range", min: 0, max: flowFrames.length - 1, value: frameIdx, style: "flex:1;min-width:160px;" });

        function syncReplayUi() {
          modeReplayBtn.classList.toggle("active", replayMode === "replay");
          modeSummaryBtn.classList.toggle("active", replayMode === "summary");
          playBtn.textContent = playing ? "Pause" : "Play";
          slider.value = String(frameIdx);
          const f = flowFrames[frameIdx];
          timeLabel.textContent = replayMode === "replay" && f ? `t = ${secFmt(f.t)} / ${secFmt(flowFrames[flowFrames.length - 1].t)}` : "Window averages";
          scrubberRow.style.display = replayMode === "replay" ? "flex" : "none";
          drawFlowStageCards();
        }
        function stopPlayback() {
          playing = false;
          if (timer) {
            clearInterval(timer);
            timer = null;
          }
        }
        modeReplayBtn.addEventListener("click", () => {
          replayMode = "replay";
          syncReplayUi();
        });
        modeSummaryBtn.addEventListener("click", () => {
          stopPlayback();
          replayMode = "summary";
          syncReplayUi();
        });
        playBtn.addEventListener("click", () => {
          if (playing) {
            stopPlayback();
          } else {
            replayMode = "replay";
            playing = true;
            timer = setInterval(() => {
              if (!document.body.contains(playBtn)) {
                stopPlayback();
                return;
              }
              frameIdx = (frameIdx + 1) % flowFrames.length;
              syncReplayUi();
            }, 900);
          }
          syncReplayUi();
        });
        slider.addEventListener("input", (e) => {
          stopPlayback();
          replayMode = "replay";
          frameIdx = Number(e.target.value);
          syncReplayUi();
        });
        ctrlBar.append(modeReplayBtn, modeSummaryBtn, playBtn, timeLabel);
        scrubberRow.append(slider);
        syncReplayUi();
        flowWrap.append(ctrlBar, scrubberRow, cardsGrid);
      } else {
        drawFlowStageCards();
        flowWrap.append(cardsGrid);
      }
      flowBody = flowWrap;
    } else {
      flowBody = el("div", { className: "empty-note", textContent: avail ? "No active server traffic frames recorded." : unavailableMsg });
    }
    pageNode.appendChild(makeSection("s-flow", "Topology & Traffic Replay", "Pipeline stages and time-stepped traffic replay", flowBody));

    // 2. #s-latency · Latency (Server & Stage Breakdown)
    const f = d.forensics || {};
    const slbView = buildOfflineStageLatencyView(f, st, cs, srvPts);
    const legacyPooledPd = Boolean(slbView.isLegacyPooledPd || isLegacyPooledPd(f, st));
    const isSrvStageSrc = slbView.source === "server_telemetry";
    const slbP50 = slbView.slices && slbView.slices.p50;
    const slbP90 = slbView.slices && slbView.slices.p90;
    const sKpi50 = isSrvStageSrc ? slbP50 : null;
    const sKpi90 = isSrvStageSrc ? slbP90 : null;
    const hasKvKpi = (sKpi50 && isFiniteMetric(sKpi50.kv_transfer_ms)) || (sKpi90 && isFiniteMetric(sKpi90.kv_transfer_ms));
    const stageKpiTile = legacyPooledPd
      ? kpiTile(
          "Stage P50 (KV Transfer / P/D TTFT / Decode Est.)",
          `${msFmt(slbP50 && slbP50.kv_transfer_ms)} / ${msFmt(slbP50 && (slbP50.ttft_unattributed_ms ?? slbP50.ttft_stage_ms))} / ${msFmt(slbP50 && slbP50.decode_time_ms)}`,
          `P90: ${msFmt(slbP90 && slbP90.kv_transfer_ms)} / ${msFmt(slbP90 && (slbP90.ttft_unattributed_ms ?? slbP90.ttft_stage_ms))} / ${msFmt(slbP90 && slbP90.decode_time_ms)}`
        )
      : kpiTile(
          "Stage P50 (Queue / Prefill / Decode)",
          `${msFmt((sKpi50 && sKpi50.queue_time_ms) ?? podStatMean(summary, "vllm_queue_time_p50_ms"))} / ${msFmt((sKpi50 && sKpi50.prefill_time_ms) ?? podStatMean(summary, "vllm_prefill_time_p50_ms"))} / ${msFmt((sKpi50 && sKpi50.decode_time_ms) ?? podStatMean(summary, "vllm_decode_time_p50_ms"))}`,
          `P90: ${msFmt((sKpi90 && sKpi90.queue_time_ms) ?? podStatMean(summary, "vllm_queue_time_p90_ms"))} / ${msFmt((sKpi90 && sKpi90.prefill_time_ms) ?? podStatMean(summary, "vllm_prefill_time_p90_ms"))} / ${msFmt((sKpi90 && sKpi90.decode_time_ms) ?? podStatMean(summary, "vllm_decode_time_p90_ms"))}${hasKvKpi ? ` | NIXL P50/P90: ${msFmt(sKpi50 && sKpi50.kv_transfer_ms)} / ${msFmt(sKpi90 && sKpi90.kv_transfer_ms)}` : ""}`
        );
    const latKpis = el("div", { className: "grid-kpis", style: "margin-bottom:12px" }, [
      legacyPooledPd
        ? kpiTile("Server TTFT (P50)", "—", "Legacy pooled P/D histograms (unseparated)")
        : kpiTile("Server TTFT (P50)", msFmt(podStatMean(summary, "vllm_ttft_p50_ms")), `P90: ${msFmt(podStatMean(summary, "vllm_ttft_p90_ms"))} | P99: ${msFmt(podStatMean(summary, "vllm_ttft_p99_ms"))}`),
      kpiTile("Server TPOT (P50)", tpotFmt(podStatMean(summary, "vllm_tpot_p50_ms")), `P90: ${tpotFmt(podStatMean(summary, "vllm_tpot_p90_ms"))} | P99: ${tpotFmt(podStatMean(summary, "vllm_tpot_p99_ms"))}`),
      legacyPooledPd
        ? kpiTile("Server E2E (P50)", "—", "Legacy pooled P/D histograms (unseparated)")
        : kpiTile("Server E2E (P50)", msFmt(podStatMean(summary, "vllm_e2e_p50_ms")), `P90: ${msFmt(podStatMean(summary, "vllm_e2e_p90_ms"))} | P99: ${msFmt(podStatMean(summary, "vllm_e2e_p99_ms"))}`),
      stageKpiTile,
    ]);
    let srvLatMode = "ttft";
    const srvLatTabs = el("div", { className: "filter-bar" });
    const srvLatHost = el("div", { className: "chart-box" });
    const srvLatWrap = el("div", null, [
      legacyPooledPd
        ? el("div", {
            className: "stage-info-note",
            textContent: "Legacy P/D server telemetry pooled Prefill and Decode histograms across roles, distorting server TTFT and E2E curves. TPOT remains Decode-specific; refresh telemetry or re-run for role-separated curves.",
          })
        : null,
      srvLatHost,
    ]);
    const stageHost = el("div", { className: "chart-box" });
    const stageNotice = el("div");
    const stageWrap = el("div", null, [stageNotice, stageHost]);
    const sStageControls = el("div", { className: "stage-controls" });
    let sStageQuantile = slbView.primarySlice === "p90" ? "p90" : "p50";
    let sP50Btn = null;
    let sP90Btn = null;

    function drawServingStageChart() {
      const useP90 = sStageQuantile === "p90" && slbView.hasP90;
      if (sP50Btn) sP50Btn.classList.toggle("active", !useP90);
      if (sP90Btn) sP90Btn.classList.toggle("active", useP90);
      const activeKey = useP90 && slbView.slices.p90 && slbView.slices.p90.available
        ? "p90"
        : (slbView.slices.p50 && slbView.slices.p50.available ? "p50" : (slbView.slices.mean && slbView.slices.mean.available ? "mean" : "p90"));
      const activeSl = (slbView.slices && slbView.slices[activeKey]) || null;
      const notices = [];
      const cav = (activeSl && activeSl.caveat) || slbView.caveat;
      if (cav) notices.push(el("div", { className: "stage-info-note", textContent: cav }));
      const rec = activeSl && activeSl.reconciliation;
      if (rec && isFiniteMetric(rec.e2e_gap_pct) && Math.abs(Number(rec.e2e_gap_pct)) >= 15) {
        const sign = Number(rec.e2e_gap_ms) >= 0 ? "+" : "";
        const pctSign = Number(rec.e2e_gap_pct) >= 0 ? "+" : "";
        notices.push(
          el("div", {
            className: "kpi-sub",
            style: "margin-bottom:8px",
            textContent: `E2E reconciliation: stage sum (${msFmt(rec.total_stage_ms)}) vs Client E2E (${msFmt(rec.client_e2e_ms)}), gap ${sign}${msFmt(rec.e2e_gap_ms)} (${pctSign}${pct(rec.e2e_gap_pct)})${rec.quantile_non_additive ? " — marginal quantiles (P50/P90) are non-additive across stages." : "."}`,
          })
        );
      }
      stageNotice.replaceChildren(...notices);
      const pts = slbView.series || [];
      const layers = offlineStageAreaLayers(slbView.source, slbView.hasKvTransfer);
      const pickVal = (pt, l) => {
        if (!pt) return null;
        const primary = useP90 ? pt[l.p90Key] : pt[l.key];
        if (isFiniteMetric(primary)) return primary;
        if (useP90 && l.fallbackP90Key && isFiniteMetric(pt[l.fallbackP90Key])) return pt[l.fallbackP90Key];
        if (!useP90 && l.fallbackKey && isFiniteMetric(pt[l.fallbackKey])) return pt[l.fallbackKey];
        return primary;
      };
      const hasSeries = pts.some((pt) => layers.some((l) => isFiniteMetric(pickVal(pt, l))));
      StackedAreaChart(stageHost, {
        emptyText: "Stage breakdown time series (queue / prefill / decode) unavailable.",
        yUnit: "ms",
        yFormat: msFmt,
        series: hasSeries
          ? layers.map((l) => ({
              label: l.label,
              color: l.color,
              ...(l.dashed ? { dashed: true } : {}),
              points: pts.map((pt) => ({ x: pt.t, y: pickVal(pt, l) })),
            }))
          : [],
      });
    }

    function drawServerLatency() {
      const prefix = srvLatMode === "ttft" ? "vllm_ttft" : srvLatMode === "tpot" ? "vllm_tpot" : "vllm_e2e";
      const label = srvLatMode.toUpperCase();
      const slos = resolveConfiguredSlos(d);
      const sloLine = srvLatMode === "tpot"
        ? slos.tpot_ms
        : srvLatMode === "e2e"
          ? slos.e2e_ms
          : slos.ttft_ms;
      const sloLabel = sloLine !== null ? `SLO: ${msFmt(sloLine)}` : null;
      const sloLegendLabel = sloLine !== null ? `${label} SLO (${msFmt(sloLine)})` : null;
      LineBandChart(srvLatHost, {
        emptyText: `Per-scrape vLLM ${label} histogram percentiles unavailable.`,
        sloLine,
        sloLabel,
        sloLegendLabel,
        series: srvPts.length
          ? [{
              label: `Server ${label} P50 (${srvLatMode === "tpot" ? "ms/tok" : "ms"})`,
              color: PALETTE[0],
              points: srvPts.map((p) => ({ x: p._t, y: p[`${prefix}_p50_ms`], lo: p[`${prefix}_p50_ms`], hi: p[`${prefix}_p90_ms`] })),
            }]
          : [],
      });
    }
    [
      { id: "ttft", label: "TTFT" },
      { id: "tpot", label: "TPOT" },
      { id: "e2e", label: "E2E" },
    ].forEach((m) => {
      srvLatTabs.appendChild(
        el("button", {
          type: "button",
          className: `pill-btn${srvLatMode === m.id ? " active" : ""}`,
          textContent: m.label,
          onClick: () => {
            srvLatMode = m.id;
            for (const b of srvLatTabs.querySelectorAll("button")) b.classList.toggle("active", b.textContent === m.label);
            drawServerLatency();
          },
        })
      );
    });
    drawServerLatency();
    if (slbView.hasP90) {
      sP50Btn = el("button", {
        type: "button",
        className: `pill-btn${sStageQuantile === "p50" ? " active" : ""}`,
        textContent: "P50",
        onClick: () => {
          sStageQuantile = "p50";
          drawServingStageChart();
        },
      });
      sP90Btn = el("button", {
        type: "button",
        className: `pill-btn${sStageQuantile === "p90" ? " active" : ""}`,
        textContent: "P90",
        onClick: () => {
          sStageQuantile = "p90";
          drawServingStageChart();
        },
      });
      sStageControls.append(sP50Btn, sP90Btn);
    }
    drawServingStageChart();
    const latChartsGrid = el("div", { className: "grid-2" }, [
      chartCard("Server Latency Percentiles over Time", srvLatWrap, null, srvLatTabs),
      chartCard("Request Stage Latency Breakdown", stageWrap, null, slbView.hasP90 ? sStageControls : null),
    ]);
    const promptSplitHost = el("div", { className: "chart-box" });
    const hasPromptSplitSeries = srvPts.some((p, i) =>
      isFiniteMetric(prefillLocalCacheTps(p, i))
      || isFiniteMetric(prefillExtTps(p, i))
      || isFiniteMetric(prefillFreshTps(p, i))
    );
    StackedAreaChart(promptSplitHost, {
      emptyText: "Prompt prefix cache vs prefill compute time series unavailable.",
      yUnit: "tok/s",
      series: hasPromptSplitSeries
        ? [
            {
              label: "Prefix Cache (tok/s)",
              color: PALETTE[1],
              points: srvPts.map((p, i) => {
                const lc = prefillLocalCacheTps(p, i);
                const ek = prefillExtTps(p, i);
                return {
                  x: p._t,
                  y: (lc !== null || ek !== null) ? Math.max(0, lc || 0) + Math.max(0, ek || 0) : null,
                };
              }),
            },
            { label: "Prefill Compute (tok/s)", color: PALETTE[2], points: srvPts.map((p, i) => ({ x: p._t, y: prefillFreshTps(p, i) })) },
          ]
        : [],
    });
    const promptSplitCard = el("div", { style: "margin-top:12px" }, [
      chartCard("Prompt Token Throughput: Prefix Cache vs Prefill Compute", promptSplitHost),
    ]);
    pageNode.appendChild(makeSection("s-latency", "Latency · Server & Stage Breakdown", "Server-side latency percentiles, stage times, and prompt source split", el("div", null, [latKpis, latChartsGrid, promptSplitCard])));

    // 3. #s-engine · Engine · vLLM
    const engPrefillSeries = srvPts.map((p, i) => isDisaggregated
      ? firstFinite(roleLookup.at(p, i).prefill_input_token_rate_tps, p.input_token_rate_tps)
      : firstFinite(p.input_token_rate_tps));
    const engDecodeSeries = srvPts.map((p, i) => isDisaggregated
      ? firstFinite(roleLookup.at(p, i).decode_output_token_rate_tps, p.generation_tokens_per_second, p.output_token_rate_tps)
      : firstFinite(p.generation_tokens_per_second, p.output_token_rate_tps));
    const engPrefillMean = isDisaggregated
      ? (podStatMean(sbr.prefill, "input_token_rate_tps")
          ?? firstFinite(roleSum.prefill && roleSum.prefill.input_token_rate_tps)
          ?? meanFiniteList(engPrefillSeries)
          ?? podStatMean(summary, "input_token_rate_tps"))
      : podStatMean(summary, "input_token_rate_tps");
    const engPrefillPeak = isDisaggregated
      ? ((sbr.prefill && statMax(sbr.prefill.input_token_rate_tps))
          ?? maxFiniteList(engPrefillSeries)
          ?? (summary.input_token_rate_tps && summary.input_token_rate_tps.max))
      : (summary.input_token_rate_tps && summary.input_token_rate_tps.max);
    const engDecodeMean = isDisaggregated
      ? (podStatMean(sbr.decode, "output_token_rate_tps")
          ?? firstFinite(roleSum.decode && roleSum.decode.output_token_rate_tps)
          ?? meanFiniteList(engDecodeSeries)
          ?? podStatMean(summary, "output_token_rate_tps"))
      : podStatMean(summary, "output_token_rate_tps");
    const engDecodePeak = isDisaggregated
      ? ((sbr.decode && statMax(sbr.decode.output_token_rate_tps))
          ?? maxFiniteList(engDecodeSeries)
          ?? (summary.output_token_rate_tps && summary.output_token_rate_tps.max))
      : (summary.output_token_rate_tps && summary.output_token_rate_tps.max);

    const engKpis = el("div", { className: "grid-kpis", style: "margin-bottom:12px" }, [
      kpiTile("Running Requests", num(podStatMean(summary, "running_requests"), 1), `Peak: ${num(summary.running_requests && summary.running_requests.max, 1)}`),
      kpiTile("Waiting Requests", num(podStatMean(summary, "queue_depth") ?? podStatMean(summary, "waiting_requests"), 1), `Peak: ${num((summary.queue_depth && summary.queue_depth.max) ?? (summary.waiting_requests && summary.waiting_requests.max), 1)}`),
      kpiTile("Prefill Throughput", `${num(engPrefillMean, 1)} tok/s`, `Peak: ${num(engPrefillPeak, 1)} tok/s`),
      kpiTile("Decode Throughput", `${num(engDecodeMean, 1)} tok/s`, `Peak: ${num(engDecodePeak, 1)} tok/s`),
    ]);
    const engQueueHost = el("div", { className: "chart-box" });
    const engTokHost = el("div", { className: "chart-box" });
    LineBandChart(engQueueHost, {
      emptyText: unavailableMsg,
      series: srvPts.length
        ? (hasDisaggRoles
            ? [
                { label: "Prefill Running", color: PALETTE[0], points: srvPts.map((p, i) => ({ x: p._t, y: roleLookup.at(p, i).prefill_running_requests })) },
                { label: "Decode Running", color: PALETTE[1], points: srvPts.map((p, i) => ({ x: p._t, y: roleLookup.at(p, i).decode_running_requests })) },
                { label: "Prefill Waiting", color: PALETTE[2], dashed: true, points: srvPts.map((p, i) => ({ x: p._t, y: roleLookup.at(p, i).prefill_waiting_requests })) },
                { label: "Decode Waiting", color: PALETTE[4], dashed: true, points: srvPts.map((p, i) => ({ x: p._t, y: roleLookup.at(p, i).decode_waiting_requests })) },
              ]
            : [
                { label: "vLLM Running Requests", color: PALETTE[0], points: srvPts.map((p) => ({ x: p._t, y: p.running_requests })) },
                { label: "vLLM Waiting Requests", color: PALETTE[2], points: srvPts.map((p) => ({ x: p._t, y: p.waiting_requests ?? p.queue_depth })) },
                { label: "EPP In-Flight", color: PALETTE[1], dashed: true, points: srvPts.map((p) => ({ x: p._t, y: p.epp_running_requests ?? p.epp_inflight_requests })) },
              ])
        : [],
    });
    LineBandChart(engTokHost, {
      emptyText: unavailableMsg,
      series: srvPts.length
        ? [
            { label: "Prefill (tok/s)", color: PALETTE[1], points: srvPts.map((p, i) => ({ x: p._t, y: engPrefillSeries[i] })) },
            { label: "Decode (tok/s)", color: PALETTE[0], points: srvPts.map((p, i) => ({ x: p._t, y: engDecodeSeries[i] })) },
          ]
        : [],
    });
    const engTpotHost = el("div", { className: "chart-box", style: "margin-top:12px" });
    LineBandChart(engTpotHost, {
      emptyText: "vLLM TPOT over time unavailable.",
      series: srvPts.some((p) => isFiniteMetric(p.vllm_tpot_p50_ms) || isFiniteMetric(p.vllm_tpot_p90_ms))
        ? [
            { label: "TPOT P50 (ms/tok)", color: PALETTE[0], points: srvPts.map((p) => ({ x: p._t, y: p.vllm_tpot_p50_ms })) },
            { label: "TPOT P90 (ms/tok)", color: PALETTE[1], dashed: true, points: srvPts.map((p) => ({ x: p._t, y: p.vllm_tpot_p90_ms })) },
          ]
        : [],
    });

    const tokenFlowData = deriveTokenFlowData(st, cs, d.sessions_summary || {}, srvPts, summary, derived, engDurationSec, engStepSec, isDisaggregated, roleSourcesData);
    const tokenFlowSegs = tokenFlowData
      ? [
          { label: "Local Prefix Cache", value: tokenFlowData.cached_tokens, color: PALETTE[1] },
          { label: "Recomputed Prefix", value: tokenFlowData.recomputed_tokens, color: PALETTE[2] },
          { label: "Uncached Prompt", value: tokenFlowData.cold_new_tokens, color: PALETTE[0] },
        ]
      : [];
    const tokenFlowHost = el("div", { className: "hbar-panel" }, [
      el("div", { className: "chart-subtitle", textContent: "Local Prefix Cache · Recomputed Prefix · Uncached Prompt (vs theoretical cache ceiling)" }),
      tokenFlowData
        ? HorizontalStackedBar({
            headerText: "Prompt Token Breakdown",
            totalValue: tokenFlowData.sent_tokens,
            totalText: `${intFmt(tokenFlowData.sent_tokens)} tok`,
            ceilingPct: tokenFlowData.theoretical_ceiling_pct,
            segments: tokenFlowSegs,
          })
        : el("div", { className: "empty-note", textContent: "Prompt token breakdown unavailable." }),
      tokenFlowData ? renderHbarLegend(tokenFlowSegs) : null,
      tokenFlowData
        ? renderStatStrip([
            { label: "Total", value: `${intFmt(tokenFlowData.sent_tokens)} tok` },
            { label: "Local Prefix Cache", value: `${intFmt(tokenFlowData.cached_tokens)} (${pct(tokenFlowData.cached_pct)})` },
            { label: "Recomputed Prefix", value: `${intFmt(tokenFlowData.recomputed_tokens)} (${pct(tokenFlowData.recomputed_pct)})` },
            { label: "Uncached Prompt", value: `${intFmt(tokenFlowData.cold_new_tokens)} (${pct(tokenFlowData.cold_new_pct)})` },
            isFiniteMetric(tokenFlowData.theoretical_ceiling_pct)
              ? { label: "Ceiling", value: pct(clampPct(tokenFlowData.theoretical_ceiling_pct)) }
              : null,
          ])
        : null,
    ]);

    const promptPerReqData = derivePromptTokensPerReqData(cs, summary, derived, tokenFlowData, isDisaggregated, roleSourceMetrics.prefillAvoidedPct);
    const promptPerReqSegs = promptPerReqData
      ? [
          { label: "Prefix Cache", value: promptPerReqData.mean_cached_tokens, color: PALETTE[1] },
          { label: "Prefill Compute", value: promptPerReqData.mean_computed_tokens, color: PALETTE[2] },
        ]
      : [];
    const promptPerReqHost = el("div", { className: "hbar-panel" }, [
      el("div", { className: "chart-subtitle", textContent: "Mean prefix-cached vs prefill-computed prompt tokens per request" }),
      promptPerReqData
        ? HorizontalStackedBar({
            headerText: "Prompt Tokens per Request",
            totalValue: promptPerReqData.mean_prompt_tokens,
            totalText: `${num(promptPerReqData.mean_prompt_tokens, 1)} tok`,
            formatValue: (v) => num(v, 1),
            segments: promptPerReqSegs,
          })
        : el("div", { className: "empty-note", textContent: "Per-request prompt token breakdown unavailable." }),
      promptPerReqData ? renderHbarLegend(promptPerReqSegs) : null,
      promptPerReqData
        ? renderStatStrip([
            { label: "Mean", value: `${num(promptPerReqData.mean_prompt_tokens, 1)} tok` },
            { label: "Prefix Cache", value: `${num(promptPerReqData.mean_cached_tokens, 1)} tok (${pct(promptPerReqData.cached_pct)})` },
            { label: "Prefill Compute", value: `${num(promptPerReqData.mean_computed_tokens, 1)} tok` },
            { label: "P50 / P90 Prompt", value: `${intFmt(promptPerReqData.prompt_p50)} / ${intFmt(promptPerReqData.prompt_p90)} tok` },
            (isFiniteMetric(promptPerReqData.computed_p50) && isFiniteMetric(promptPerReqData.computed_p90))
              ? { label: "P50 / P90 Prefill Compute", value: `${intFmt(promptPerReqData.computed_p50)} / ${intFmt(promptPerReqData.computed_p90)} tok` }
              : null,
          ])
        : null,
    ]);

    const roleSourceLegend = [
      { label: "Local Prefix Cache", color: PALETTE[1] },
      { label: "External KV Transfer", color: PALETTE[3] },
      { label: "Prefill Compute", color: PALETTE[2] },
    ];
    const roleSourceHost = el("div", { className: "hbar-panel" }, [
      el("div", { className: "chart-subtitle", textContent: "Local Prefix Cache · External KV Transfer · Prefill Compute" }),
      roleSourcesData.length
        ? el(
            "div",
            { className: "role-src-list" },
            roleSourcesData.map((row) => {
              const freshPct = row.total_tokens > 0 ? (row.local_compute_tokens / row.total_tokens) * 100 : 0;
              return el("div", { className: "role-src-row" }, [
                el("span", { className: "badge info", textContent: row.role }),
                HorizontalStackedBar({
                  headerText: `Role: ${row.role}`,
                  totalValue: row.total_tokens,
                  totalText: `${intFmt(row.total_tokens)} tok`,
                  compact: true,
                  segments: [
                    { label: "Local Prefix Cache", value: row.local_cache_tokens, color: PALETTE[1] },
                    { label: "External KV Transfer", value: row.external_kv_tokens, color: PALETTE[3] },
                    { label: "Prefill Compute", value: row.local_compute_tokens, color: PALETTE[2] },
                  ],
                }),
                el("span", { className: "role-src-meta", textContent: `${intFmt(row.total_tokens)} tok · Prefill Compute ${pct(freshPct)}` }),
              ]);
            })
          )
        : el("div", { className: "empty-note", textContent: "Prompt token sources by role unavailable." }),
      roleSourcesData.length ? renderHbarLegend(roleSourceLegend) : null,
    ]);

    const podTable = cleanedPods.length
      ? el("div", { style: "margin-top:12px" }, [
          makeSortableTable(
            [
              { key: "pod", label: "Pod", getter: (r) => r.pod || r.name },
              { key: "role", label: "Role", getter: (r) => effectiveReportPodRole(r) },
              { key: "rps", label: "Req/s", num: true, getter: (r) => podStatMean(r, "request_rate_rps"), format: (v) => num(v, 2) },
              { key: "in_tps", label: "Prefill (tok/s)", num: true, getter: (r) => podStatMean(r, "input_token_rate_tps"), format: (v) => num(v, 1) },
              { key: "out_tps", label: "Decode (tok/s)", num: true, getter: (r) => podStatMean(r, "output_token_rate_tps"), format: (v) => num(v, 1) },
              { key: "run", label: "Running", num: true, getter: (r) => podStatMean(r, "running_requests"), format: (v) => num(v, 1) },
              { key: "kv", label: "KV Cache (%)", num: true, getter: (r) => (r.kv_cache_usage_percent && r.kv_cache_usage_percent.max) ?? podStatMean(r, "kv_cache_usage_percent"), format: pct },
            ],
            cleanedPods
          ),
        ])
      : null;
    pageNode.appendChild(
      makeSection(
        "s-engine",
        "Engine · vLLM",
        `Topology mode: ${topoLabel}`,
        el("div", null, [
          engKpis,
          el("div", { className: "grid-2" }, [
            chartCard("Running vs Waiting Requests over Time", engQueueHost),
            chartCard("Prefill vs Decode Token Throughput", engTokHost),
          ]),
          el("div", { style: "margin-top:12px" }, [
            chartCard(
              "vLLM TPOT over Time",
              engTpotHost,
              null,
              el("span", { className: "chart-subtitle", textContent: "Server TPOT P50 and P90 (ms/tok); falls back to vLLM ITL when TPOT histogram is absent" })
            ),
          ]),
          el("div", { className: "grid-2", style: "margin-top:12px" }, [
            chartCard("Prompt Token Breakdown", tokenFlowHost),
            chartCard("Prompt Tokens per Request", promptPerReqHost),
          ]),
          el("div", { style: "margin-top:12px" }, [chartCard("Prompt Token Sources by Role", roleSourceHost)]),
          podTable,
        ])
      )
    );

    // 4. #s-load · Load (Gateway & EPP Routing)
    const pdDecisionRatio = isFiniteMetric(derived.pd_decision_ratio) ? Number(derived.pd_decision_ratio) : null;
    const isPdRoutingTile = isDisaggTopo || pdDecisionRatio !== null;
    const routerCachedTps = podStatMean(router, "cached_token_rate_tps");
    const routerInputTps = podStatMean(router, "input_token_rate_tps");
    const fallbackEppCacheHit = isFiniteMetric(routerInputTps) && Number(routerInputTps) > 0 && isFiniteMetric(routerCachedTps)
      ? clampPct((Number(routerCachedTps) / Number(routerInputTps)) * 100)
      : null;
    const eppPredictedCacheHit = isFiniteMetric(derived.avoided_prefill_ratio) ? clampPct(derived.avoided_prefill_ratio) : fallbackEppCacheHit;
    const prefixIndexEntries = podStatMean(router, "index_entry_count");
    const istioP90Ms = podStatMean(router, "istio_latency_p90_ms") ?? podStatMean(router, "istio_request_duration_p90_ms") ?? podStatMean(summary, "istio_latency_p90_ms");
    const loadKpis = el("div", { className: "grid-kpis", style: "margin-bottom:12px" }, [
      kpiTile("EPP In-Flight Requests", num(podStatMean(summary, "epp_inflight_requests"), 1), `Peak: ${num(summary.epp_inflight_requests && summary.epp_inflight_requests.max, 1)} | Ready: ${num(podStatMean(router, "ready_endpoints"), 0)}`),
      kpiTile("EPP Scheduling Latency", msFmt(podStatMean(router, "scheduler_latency_p50_ms") ?? podStatMean(router, "scheduler_e2e_p50_ms")), `P90: ${msFmt(podStatMean(router, "scheduler_latency_p90_ms") ?? podStatMean(router, "scheduler_e2e_p90_ms"))}`),
      kpiTile(
        "EPP Plugin Latency",
        msFmt(podStatMean(router, "plugin_latency_p50_ms") ?? podStatMean(router, "plugin_duration_p50_ms")),
        `P90: ${msFmt(podStatMean(router, "plugin_latency_p90_ms") ?? podStatMean(router, "plugin_duration_p90_ms"))} | Inference Gateway P90: ${istioP90Ms !== null ? msFmt(istioP90Ms) : "Unavailable"}`
      ),
      isPdRoutingTile
        ? kpiTile("P/D Routing Ratio", pdDecisionRatio !== null ? pct(pdDecisionRatio) : "N/A", `Avoided Prefill: ${pct(derived.avoided_prefill_ratio)} | Mode: P/D Disaggregation`)
        : kpiTile(
            "EPP Predicted Prefix Cache Hit Rate",
            eppPredictedCacheHit !== null ? pct(eppPredictedCacheHit) : "Aggregated",
            `Routing Mode: Aggregated (100% Worker) | Prefix Index Entries: ${prefixIndexEntries !== null ? intFmt(prefixIndexEntries) : "—"}`
          ),
    ]);
    const loadRateHost = el("div", { className: "chart-box" });
    const loadLatHost = el("div", { className: "chart-box" });
    LineBandChart(loadRateHost, {
      emptyText: avail ? "EPP load series unavailable." : unavailableMsg,
      rightLabel: "req/s",
      series: srvPts.some((p, i) =>
        isFiniteMetric(p.router_request_rate_rps)
        || isFiniteMetric(p.epp_inflight_requests)
        || isFiniteMetric(p.request_rate_rps)
        || (isDisaggregated && (isFiniteMetric(roleLookup.at(p, i).decode_request_rate_rps) || isFiniteMetric(roleLookup.at(p, i).prefill_request_rate_rps)))
      )
        ? [
            { label: "EPP In-Flight (req)", color: PALETTE[3], points: srvPts.map((p) => ({ x: p._t, y: p.epp_inflight_requests ?? p.router_running_requests })) },
            { label: "EPP Request Rate (req/s)", axis: "right", color: PALETTE[0], points: srvPts.map((p) => ({ x: p._t, y: p.router_request_rate_rps ?? p.router_scheduler_attempt_rate_rps })) },
            {
              label: "vLLM Request Rate (req/s)",
              axis: "right",
              color: PALETTE[1],
              dashed: true,
              points: srvPts.map((p, i) => ({
                x: p._t,
                y: isDisaggregated
                  ? firstFinite(roleLookup.at(p, i).decode_request_rate_rps, roleLookup.at(p, i).prefill_request_rate_rps, p.request_rate_rps)
                  : p.request_rate_rps,
              })),
            },
          ]
        : [],
    });
    const hasLoadLatSeries = srvPts.some((p) => (
      isFiniteMetric(p.router_scheduler_latency_p50_ms ?? p.router_scheduler_e2e_p50_ms)
      || isFiniteMetric(p.router_scheduler_latency_p90_ms ?? p.router_scheduler_e2e_p90_ms)
      || isFiniteMetric(p.router_plugin_latency_p50_ms ?? p.plugin_duration_p50_ms)
      || isFiniteMetric(p.router_plugin_latency_p90_ms ?? p.plugin_duration_p90_ms)
    ));
    LineBandChart(loadLatHost, {
      emptyText: "EPP scheduling & plugin latency series unavailable.",
      series: hasLoadLatSeries
        ? [
            { label: "EPP Scheduling P50 (ms)", color: PALETTE[0], points: srvPts.map((p) => ({ x: p._t, y: p.router_scheduler_latency_p50_ms ?? p.router_scheduler_e2e_p50_ms })) },
            { label: "EPP Scheduling P90 (ms)", color: PALETTE[1], dashed: true, points: srvPts.map((p) => ({ x: p._t, y: p.router_scheduler_latency_p90_ms ?? p.router_scheduler_e2e_p90_ms })) },
            { label: "EPP Plugin P50 (ms)", color: PALETTE[2], points: srvPts.map((p) => ({ x: p._t, y: p.router_plugin_latency_p50_ms ?? p.plugin_duration_p50_ms })) },
            { label: "EPP Plugin P90 (ms)", color: PALETTE[4], dashed: true, points: srvPts.map((p) => ({ x: p._t, y: p.router_plugin_latency_p90_ms ?? p.plugin_duration_p90_ms })) },
          ]
        : [],
    });
    const epDistRows = resolveEndpointDistributionRows(st, derived, cleanedPods, perEndpoint, isDisaggregated);
    const effectiveTokenLoadCv = resolveEffectiveTokenLoadCv(derived, perEndpoint, cleanedPods, isDisaggregated);
    const epColumns = [
      { key: "endpoint", label: "Destination Endpoint", getter: (r) => r.endpoint || r.pod },
      ...(isDisaggregated
        ? [
            {
              key: "role",
              label: "Role",
              getter: (r) => r.role || "worker",
              render: (raw) => {
                const rk = String(raw || "worker").toLowerCase();
                const cls = rk === "prefill" ? "info" : (rk === "decode" ? "violet" : "");
                const lbl = rk === "prefill" ? "Prefill" : (rk === "decode" ? "Decode" : "Worker");
                return el("span", { className: `badge ${cls}`.trim(), textContent: lbl });
              },
            },
          ]
        : []),
      { key: "percent", label: "Share %", num: true, format: pct },
      { key: "request_rate_rps", label: "Rate (req/s)", num: true, format: (v) => num(v, 2) },
      { key: "inflight", label: "In-Flight Token Load", num: true, getter: (r) => r.inflight_token_load && typeof r.inflight_token_load === "object" ? r.inflight_token_load.mean : r.inflight_token_load, format: (v) => num(v, 1) },
    ];
    const epTable = epDistRows.length
      ? el("div", { style: "margin-top:12px" }, [
          effectiveTokenLoadCv !== null
            ? renderStatStrip([{ label: isDisaggregated ? "Intra-Role Token Load CV" : "Token Load CV", value: num(effectiveTokenLoadCv, 4) }])
            : null,
          makeSortableTable(epColumns, epDistRows),
        ])
      : null;
    pageNode.appendChild(
      makeSection(
        "s-load",
        "Load · Inference Gateway & EPP Routing",
        "Inference Gateway & EPP load, scheduling latency, and endpoint share",
        el("div", null, [
          loadKpis,
          el("div", { className: "grid-2" }, [
            chartCard("EPP Load · In-Flight & Request Rate", loadRateHost),
            chartCard("EPP Scheduling & Plugin Latency", loadLatHost),
          ]),
          epTable,
        ])
      )
    );

    // 5. #s-cache · KV Cache (Capacity & Usage)
    const eng0 = cacheEngines[0] || {};
    const hbmCap = cacheConfig.hbm_capacity_tokens ?? eng0.kv_cache_size_tokens;
    const effCap = cacheConfig.effective_capacity_tokens ?? hbmCap;
    const blkSize = cacheConfig.block_size ?? eng0.block_size;
    const gpuBlocks = cacheConfig.num_gpu_blocks ?? eng0.num_gpu_blocks;
    const maxConc = cacheConfig.kv_cache_max_concurrency ?? eng0.kv_cache_max_concurrency;
    const podRoleMapForCap = buildPodRoleMap(cleanedPods);
    let prefillHbmCap = positiveFinite(cacheConfig.prefill_hbm_capacity_tokens);
    let decodeHbmCap = positiveFinite(cacheConfig.decode_hbm_capacity_tokens);
    if (isDisaggregated && (prefillHbmCap === null || decodeHbmCap === null) && cacheEngines.length) {
      let pCapSum = 0;
      let dCapSum = 0;
      for (const eng of cacheEngines) {
        const tok = positiveFinite(eng && eng.kv_cache_size_tokens);
        if (tok === null) continue;
        const r = inferEndpointRole({ pod: eng.pod || eng.instance }, podRoleMapForCap);
        if (r === "prefill") pCapSum += tok;
        else if (r === "decode") dCapSum += tok;
      }
      if (prefillHbmCap === null && pCapSum > 0) prefillHbmCap = pCapSum;
      if (decodeHbmCap === null && dCapSum > 0) decodeHbmCap = dCapSum;
    }
    const hbmCapSub = isDisaggregated && prefillHbmCap !== null && decodeHbmCap !== null
      ? `Prefill: ${intFmt(prefillHbmCap)} | Decode: ${intFmt(decodeHbmCap)} tok`
      : `Source: ${cacheConfig.source || "vLLM"}`;
    const cacheKpis = el("div", { className: "grid-kpis", style: "margin-bottom:12px" }, [
      kpiTile("KV Cache Usage (Peak / Mean)", pct(kvPeak), `Mean: ${pct(kvMean)}`),
      kpiTile("HBM Capacity", hbmCap != null ? `${intFmt(hbmCap)} tok` : "N/A", hbmCapSub),
      kpiTile("Effective Capacity", effCap != null ? `${intFmt(effCap)} tok` : "N/A", `Max KV Concurrency: ${num(maxConc, 2)}x`),
      kpiTile("Block Size & Device KV Blocks", blkSize != null ? `${intFmt(blkSize)} tok` : "N/A", `Blocks: ${intFmt(gpuBlocks)}`),
    ]);
    const kvHost = el("div", { className: "chart-box" });
    const kvChartSeries = srvPts.length
      ? (isDisaggregated && (hasDecodeKv || hasPrefillKv)
          ? [
              ...(hasDecodeKv ? [{ label: "Decode HBM KV Usage (%)", color: PALETTE[2], points: srvPts.map((p, i) => ({ x: p._t, y: decodeKvPoint(p, i) })) }] : []),
              ...(hasPrefillKv ? [{ label: "Prefill HBM KV Usage (%)", color: PALETTE[1], points: srvPts.map((p, i) => ({ x: p._t, y: prefillKvPoint(p, i) })) }] : []),
            ]
          : [{
              label: "HBM KV Cache Usage (%)",
              color: PALETTE[2],
              points: srvPts.map((p) => ({
                x: p._t,
                y: clampPct(isFiniteMetric(p.kv_cache_usage_percent) ? p.kv_cache_usage_percent : p.gpu_cache_usage_percent),
              })),
            }])
      : [];
    LineBandChart(kvHost, {
      emptyText: unavailableMsg,
      yMin: 0,
      yMax: 100,
      sloLine: 90,
      sloLabel: "Threshold: 90%",
      sloLegendLabel: "Pressure Threshold (90%)",
      series: kvChartSeries,
    });
    const tierSrvPts = isDisaggregated
      ? srvPts.map((p, i) => ({ ...p, kv_cache_usage_percent: effectiveKvPoint(p, i) }))
      : srvPts;
    const tierSummary = isDisaggregated
      ? {
          ...summary,
          kv_cache_usage_percent: {
            ...(summary.kv_cache_usage_percent || {}),
            mean: kvMean,
            max: kvPeak,
          },
        }
      : summary;
    const tierData = computeKvTierShareData(tierSrvPts, cacheConfig, tierSummary);
    const makeTierPill = (name, color, shareMean, usageMean, configured) => {
      const subText = (!configured && (usageMean === null || usageMean === 0))
        ? "Not configured"
        : `Mean Usage: ${pct(usageMean ?? 0)}`;
      return el("div", { className: "kv-tier-pill", "data-kv-tier": name }, [
        el("div", { className: "kv-tier-head" }, [
          el("span", { className: "kv-tier-dot", style: `background:${color}` }),
          el("span", { textContent: name }),
        ]),
        el("div", { className: "kv-tier-val", textContent: pct(shareMean) }),
        el("div", { className: "kv-tier-sub", textContent: subText }),
      ]);
    };
    const kvTierPills = el("div", { className: "kv-tier-grid" }, [
      makeTierPill("HBM", PALETTE[0], tierData.meanShares.hbm, tierData.meanUsage.hbm, tierData.configured.hbm),
      makeTierPill("Host Memory", PALETTE[1], tierData.meanShares.host, tierData.meanUsage.host, tierData.configured.host),
      makeTierPill("NVMe", PALETTE[2], tierData.meanShares.nvme, tierData.meanUsage.nvme, tierData.configured.nvme),
    ]);
    const kvTierChartHost = el("div", { className: "chart-box" });
    StackedAreaChart(kvTierChartHost, {
      emptyText: unavailableMsg,
      yMin: 0,
      yMax: 100,
      yTicks: [0, 25, 50, 75, 100],
      yUnit: "%",
      series: tierData.points.length
        ? [
            { label: "HBM", color: PALETTE[0], points: tierData.points.map((pt) => ({ x: pt.x, y: pt.hbm_share_pct })) },
            { label: "Host Memory (Offload)", color: PALETTE[1], points: tierData.points.map((pt) => ({ x: pt.x, y: pt.host_share_pct })) },
            { label: "NVMe (Offload)", color: PALETTE[2], points: tierData.points.map((pt) => ({ x: pt.x, y: pt.nvme_share_pct })) },
          ]
        : [],
    });
    const kvTierHost = el("div", null, [kvTierPills, kvTierChartHost]);
    const cacheEngTable = cacheEngines.length
      ? el("div", { style: "margin-top:12px" }, [
          makeSortableTable(
            [
              { key: "pod", label: "Pod", getter: (r) => r.pod || r.instance },
              { key: "engine", label: "Engine", num: true },
              { key: "block_size", label: "Block Size", num: true, format: intFmt },
              { key: "num_gpu_blocks", label: "Device KV Blocks", num: true, format: intFmt },
              { key: "kv_cache_size_tokens", label: "KV Tokens", num: true, format: intFmt },
              { key: "kv_cache_max_concurrency", label: "Max Concurrency", num: true, format: (v) => num(v, 2) },
            ],
            cacheEngines
          ),
        ])
      : null;
    pageNode.appendChild(
      makeSection(
        "s-cache",
        "KV Cache · Capacity & Usage",
        `Peak KV: ${pct(kvPeak)} | Mean: ${pct(kvMean)}`,
        el("div", null, [
          cacheKpis,
          el("div", { className: "grid-2" }, [
            chartCard("HBM KV Cache Usage over Time", kvHost),
            chartCard("KV Cache Tier Share (HBM · Host Memory · NVMe)", kvTierHost),
          ]),
          cacheEngTable,
        ])
      )
    );

    // 6. #s-kvreuse · KV Reuse
    const hitAvg = prefixHitMetrics.hitAvg;
    const hitMax = prefixHitMetrics.hitMax;
    const extHitAvg = isDisaggregated
      ? (podStatMean(sbr.decode, "external_prefix_cache_hit_percent")
          ?? meanOfRoleSeries(roleSeries, "decode_external_prefix_cache_hit_percent")
          ?? podStatMean(summary, "external_prefix_cache_hit_percent"))
      : podStatMean(summary, "external_prefix_cache_hit_percent");
    const rawLocalCacheSummary = podStatMean(summary, "engine_prompt_local_cache_tps");
    const localCacheTpsMean = isDisaggregated
      ? (podStatMean(sbr.prefill, "engine_prompt_local_cache_tps")
          ?? meanOfRoleSeries(roleSeries, "prefill_engine_prompt_local_cache_tps")
          ?? firstFinite(roleSum.prefill && roleSum.prefill.engine_prompt_local_cache_tps)
          ?? (rawLocalCacheSummary !== null ? (isPdCanonical ? rawLocalCacheSummary : rawLocalCacheSummary * prefillLocalCacheShare) : null))
      : rawLocalCacheSummary;
    const externalCachedTpsMean = isDisaggregated
      ? (podStatMean(sbr.decode, "engine_prompt_external_tps") ?? podStatMean(summary, "engine_prompt_external_tps"))
      : podStatMean(summary, "engine_prompt_external_tps");
    const freshComputeTpsMean = isDisaggregated
      ? (podStatMean(sbr.prefill, "engine_prompt_local_compute_tps")
          ?? meanOfRoleSeries(roleSeries, "prefill_engine_prompt_local_compute_tps")
          ?? firstFinite(roleSum.prefill && roleSum.prefill.engine_prompt_local_compute_tps)
          ?? podStatMean(summary, "engine_prompt_local_compute_tps"))
      : podStatMean(summary, "engine_prompt_local_compute_tps");
    const recompTpsMean = isDisaggregated
      ? (podStatMean(sbr.prefill, "engine_prompt_recomputed_tps")
          ?? meanOfRoleSeries(roleSeries, "prefill_engine_prompt_recomputed_tps")
          ?? firstFinite(roleSum.prefill && roleSum.prefill.engine_prompt_recomputed_tps)
          ?? podStatMean(summary, "engine_prompt_recomputed_tps")
          ?? derived.recomputed_token_rate_tps)
      : (podStatMean(summary, "engine_prompt_recomputed_tps") ?? derived.recomputed_token_rate_tps);
    const reuseKpis = el("div", { className: "grid-kpis", style: "margin-bottom:12px" }, [
      kpiTile("Prefix Cache Hit Rate", pct(hitAvg), `Peak: ${pct(hitMax)}`),
      kpiTile(isDisaggregated ? "Decode External KV Share (NIXL)" : "External Prefix Cache Hit Rate", pct(extHitAvg), `Avoided Prefill: ${pct(derived.avoided_prefill_ratio)}`),
      kpiTile("Local Prefix Cache Throughput", `${num(localCacheTpsMean, 1)} tok/s`, `${isDisaggregated ? "Decode External KV (NIXL)" : "External KV Transfer"}: ${num(externalCachedTpsMean, 1)} tok/s`),
      kpiTile("Prefill Compute Throughput", `${num(freshComputeTpsMean, 1)} tok/s`, `Recomputed Prefix: ${num(recompTpsMean, 1)} tok/s`),
    ]);
    const hitHost = el("div", { className: "chart-box" });
    const srcTokHost = el("div", { className: "chart-box" });
    function bucketReusablePctAt(buckets, t, fallbackCeilingPct) {
      const list = Array.isArray(buckets) ? buckets : [];
      if (list.length) {
        const match = list.find((b) => {
          if (!b || typeof b !== "object") return false;
          const s = isFiniteMetric(b.start_seconds) ? Number(b.start_seconds) : null;
          const e = isFiniteMetric(b.end_seconds) ? Number(b.end_seconds) : null;
          if (s !== null && e !== null) return t >= s && t <= e;
          const mid = isFiniteMetric(b.mid_seconds) ? Number(b.mid_seconds) : null;
          return mid !== null && Math.abs(t - mid) <= 2.5;
        });
        if (match && isFiniteMetric(match.reusable_prefix_pct)) {
          return clampPct(match.reusable_prefix_pct);
        }
        if (match && isFiniteMetric(match.arrived_requests) && Number(match.arrived_requests) > 0) {
          return null;
        }
      }
      return isFiniteMetric(fallbackCeilingPct) ? clampPct(fallbackCeilingPct) : null;
    }
    const fallbackCeilingPct = tokenFlowData && isFiniteMetric(tokenFlowData.theoretical_ceiling_pct)
      ? tokenFlowData.theoretical_ceiling_pct
      : null;
    const hitStackPts = srvPts.map((p, idx) => {
      const rp = roleLookup.at(p, idx);
      const rawPrefix = isDisaggregated
        ? firstFinite(rp.prefill_prefix_cache_hit_percent, hitPctFromPoint(p))
        : hitPctFromPoint(p);
      const rawExt = isDisaggregated
        ? (isFiniteMetric(rp.prefill_external_prefix_cache_hit_percent) ? Number(rp.prefill_external_prefix_cache_hit_percent) : null)
        : (isFiniteMetric(p.external_prefix_cache_hit_percent) ? Number(p.external_prefix_cache_hit_percent) : null);
      const rawFresh = isDisaggregated
        ? prefillFreshTps(p, idx)
        : (isFiniteMetric(p.engine_prompt_local_compute_tps) ? Number(p.engine_prompt_local_compute_tps) : null);
      const rawInput = isDisaggregated
        ? firstFinite(rp.prefill_input_token_rate_tps, p.input_token_rate_tps)
        : (isFiniteMetric(p.input_token_rate_tps) ? Number(p.input_token_rate_tps) : null);
      const rawRecompTps = isDisaggregated
        ? firstFinite(rp.prefill_engine_prompt_recomputed_tps, p.engine_prompt_recomputed_tps)
        : (isFiniteMetric(p.engine_prompt_recomputed_tps) ? Number(p.engine_prompt_recomputed_tps) : null);
      let prefixHit = null;
      let extHit = null;
      let recomputedPct = null;
      let coldNewPct = null;
      let uncachedPct = null;
      if (isFiniteMetric(rawPrefix)) {
        prefixHit = clampPct(rawPrefix);
        const maxExt = Math.max(0, 100 - prefixHit);
        extHit = rawExt !== null ? Math.min(maxExt, Math.max(0, rawExt)) : 0;
        uncachedPct = clampPct(100 - (prefixHit + extHit));
      } else if (rawExt !== null) {
        prefixHit = 0;
        extHit = clampPct(rawExt);
        uncachedPct = clampPct(100 - extHit);
      } else if (rawFresh !== null && rawInput !== null && rawInput > 0) {
        prefixHit = 0;
        extHit = 0;
        uncachedPct = clampPct((rawFresh / rawInput) * 100);
      }
      if (uncachedPct !== null) {
        const totalHitPct = clampPct((prefixHit || 0) + (extHit || 0));
        const vllmRecompPct = (rawRecompTps !== null && rawRecompTps > 0 && rawInput !== null && rawInput > 0)
          ? Math.min(uncachedPct, Math.max(0, (rawRecompTps / rawInput) * 100))
          : 0;
        const reusablePct = bucketReusablePctAt(cs.buckets, p._t, fallbackCeilingPct);
        const sessionRecompPct = reusablePct !== null
          ? Math.min(uncachedPct, Math.max(0, reusablePct - totalHitPct))
          : 0;
        recomputedPct = Math.min(uncachedPct, Math.max(0, Math.max(vllmRecompPct, sessionRecompPct)));
        coldNewPct = clampPct(uncachedPct - recomputedPct);
      }
      return { x: p._t, prefixHit, extHit, recomputedPct, coldNewPct };
    });
    const hasHitStack = hitStackPts.some((pt) => pt.prefixHit !== null || pt.extHit !== null || pt.recomputedPct !== null || pt.coldNewPct !== null);
    const hasSrcTokSeries = srvPts.some((p, i) =>
      isFiniteMetric(prefillLocalCacheTps(p, i))
      || isFiniteMetric(prefillExtTps(p, i))
      || isFiniteMetric(prefillFreshTps(p, i))
    );
    StackedAreaChart(hitHost, {
      emptyText: "Prefix cache hit series unavailable.",
      yMin: 0,
      yMax: 100,
      yTicks: [0, 25, 50, 75, 100],
      yUnit: "%",
      series: hasHitStack
        ? [
            { label: "Local Prefix Cache (%)", color: PALETTE[0], points: hitStackPts.map((pt) => ({ x: pt.x, y: pt.prefixHit })) },
            { label: "External KV Transfer (%)", color: PALETTE[3], points: hitStackPts.map((pt) => ({ x: pt.x, y: pt.extHit })) },
            { label: "Recomputed Prefix (%)", color: PALETTE[2], points: hitStackPts.map((pt) => ({ x: pt.x, y: pt.recomputedPct })) },
            { label: "Uncached Prompt (%)", color: PALETTE[4], points: hitStackPts.map((pt) => ({ x: pt.x, y: pt.coldNewPct })) },
          ]
        : [],
    });
    StackedAreaChart(srcTokHost, {
      emptyText: "Prompt token source series unavailable.",
      yUnit: "tok/s",
      series: hasSrcTokSeries
        ? [
            { label: "Local Prefix Cache", color: PALETTE[1], points: srvPts.map((p, i) => ({ x: p._t, y: prefillLocalCacheTps(p, i) })) },
            { label: "External KV Transfer", color: PALETTE[3], points: srvPts.map((p, i) => ({ x: p._t, y: prefillExtTps(p, i) })) },
            { label: "Prefill Compute", color: PALETTE[2], points: srvPts.map((p, i) => ({ x: p._t, y: prefillFreshTps(p, i) })) },
          ]
        : [],
    });
    pageNode.appendChild(
      makeSection(
        "s-kvreuse",
        "KV Reuse · Prefix Cache & Prompt Sources",
        "Local Prefix Cache · External KV Transfer · Recomputed Prefix · Uncached Prompt (%) and prompt token sources",
        el("div", null, [
          reuseKpis,
          el("div", { className: "grid-2" }, [
            chartCard("Prefix Cache Hit Rate over Time", hitHost, null, el("span", { className: "chart-subtitle", textContent: "Local Prefix Cache · External KV Transfer · Recomputed Prefix · Uncached Prompt (%)" })),
            chartCard("Prompt Token Sources over Time", srcTokHost),
          ]),
        ])
      )
    );

    // 7. #s-offload · KV Offload
    const vramPts = srvPts.map((p, i) => effectiveKvPoint(p, i)).filter((v) => v !== null);
    const cpuPts = srvPts.map((p) => clampPct(p.cpu_cache_usage_percent)).filter((v) => v !== null);
    const nvmePts = srvPts.map((p) => clampPct(p.disk_cache_usage_percent)).filter((v) => v !== null);
    const tierAvg = (arr) => (arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : null);
    const tierMax = (arr) => (arr.length ? Math.max(...arr) : null);

    const vramKvMean = isDisaggregated
      ? (kvMean ?? tierAvg(vramPts))
      : (podStatMean(summary, "kv_cache_usage_percent") ?? podStatMean(summary, "gpu_cache_usage_percent") ?? tierAvg(vramPts));
    const vramKvMax = isDisaggregated
      ? (kvPeak ?? tierMax(vramPts) ?? vramKvMean)
      : ((summary.kv_cache_usage_percent && isFiniteMetric(summary.kv_cache_usage_percent.max) ? Number(summary.kv_cache_usage_percent.max) : null)
          ?? (summary.gpu_cache_usage_percent && isFiniteMetric(summary.gpu_cache_usage_percent.max) ? Number(summary.gpu_cache_usage_percent.max) : null)
          ?? tierMax(vramPts)
          ?? vramKvMean);
    const cpuKvMean = podStatMean(summary, "cpu_cache_usage_percent") ?? tierAvg(cpuPts);
    const cpuKvMax = (summary.cpu_cache_usage_percent && isFiniteMetric(summary.cpu_cache_usage_percent.max) ? Number(summary.cpu_cache_usage_percent.max) : null)
      ?? tierMax(cpuPts)
      ?? cpuKvMean;
    const nvmeKvMean = podStatMean(summary, "disk_cache_usage_percent") ?? tierAvg(nvmePts);
    const nvmeKvMax = (summary.disk_cache_usage_percent && isFiniteMetric(summary.disk_cache_usage_percent.max) ? Number(summary.disk_cache_usage_percent.max) : null)
      ?? tierMax(nvmePts)
      ?? nvmeKvMean;

    const offBytes = isFiniteMetric(derived.offloaded_bytes) ? Number(derived.offloaded_bytes) : podStatMean(summary, "kv_offload_bytes_total");
    const restBytes = isFiniteMetric(derived.restored_bytes) ? Number(derived.restored_bytes) : podStatMean(summary, "kv_restore_bytes_total");
    const offRate = podStatMean(summary, "kv_offload_bytes_per_second");
    const offPeakRate = summary.kv_offload_bytes_per_second && isFiniteMetric(summary.kv_offload_bytes_per_second.max) ? Number(summary.kv_offload_bytes_per_second.max) : null;
    const restRate = podStatMean(summary, "kv_restore_bytes_per_second");
    const restPeakRate = summary.kv_restore_bytes_per_second && isFiniteMetric(summary.kv_restore_bytes_per_second.max) ? Number(summary.kv_restore_bytes_per_second.max) : null;
    const lookupP95Ms = podStatMean(summary, "kv_offload_lookup_latency_p95_ms") ?? (summary.kv_offload_lookup_latency_p95_ms && summary.kv_offload_lookup_latency_p95_ms.max);
    const nixlRate = podStatMean(summary, "nixl_transfer_bytes_per_second");
    const nixlP95 = podStatMean(summary, "nixl_transfer_latency_p95_ms");
    const nixlFail = podStatMean(summary, "nixl_failed_transfer_rate_rps") ?? (isFiniteMetric(summary.nixl_num_failed_transfers) ? Number(summary.nixl_num_failed_transfers) : null);
    const hasNixlSeries = srvPts.some((p) => isFiniteMetric(p.nixl_transfer_bytes_per_second ?? p.nixl_bytes_per_second));
    const hasNixlMetrics = nixlRate !== null || nixlP95 !== null || nixlFail !== null || hasNixlSeries;
    const showNixl = isDisaggTopo || hasNixlMetrics;

    const rateSub = (meanBps, peakBps) => {
      const m = meanBps !== null ? num(meanBps / (1024 ** 2), 1) : "N/A";
      const pk = peakBps !== null ? num(peakBps / (1024 ** 2), 1) : "N/A";
      return `Mean / Peak: ${m} / ${pk} MiB/s`;
    };

    const tierUsageKpis = el("div", { className: "grid-kpis", style: "margin-bottom:12px" }, [
      kpiTile("VRAM KV Cache Usage", vramKvMean != null ? pct(vramKvMean) : (vramKvMax != null ? pct(vramKvMax) : "N/A"), `Peak: ${pct(vramKvMax)}`),
      kpiTile("CPU KV Cache Usage", cpuKvMean != null ? pct(cpuKvMean) : (cpuKvMax != null ? pct(cpuKvMax) : (showNixl ? "Not enabled" : "N/A")), `Peak: ${pct(cpuKvMax)}`),
      kpiTile("NVMe KV Cache Usage", nvmeKvMean != null ? pct(nvmeKvMean) : (nvmeKvMax != null ? pct(nvmeKvMax) : (showNixl ? "Not enabled" : "N/A")), `Peak: ${pct(nvmeKvMax)}`),
    ]);

    const offloadKpis = showNixl
      ? el("div", { className: "grid-kpis", style: "margin-bottom:12px" }, [
          kpiTile("KV Offload Throughput", offBytes != null ? bytesFmt(offBytes) : (offRate != null ? `${num(offRate / (1024 ** 2), 1)} MiB/s` : "Not enabled"), rateSub(offRate, offPeakRate)),
          kpiTile("KV Restore Throughput", restBytes != null ? bytesFmt(restBytes) : (restRate != null ? `${num(restRate / (1024 ** 2), 1)} MiB/s` : "Not enabled"), rateSub(restRate, restPeakRate)),
          kpiTile("NIXL KV Transfer Throughput", nixlRate != null ? `${num(nixlRate / (1024 ** 2), 1)} MiB/s` : "Not enabled", `P95: ${msFmt(nixlP95)} | Failures: ${nixlFail != null ? intFmt(nixlFail) : "0"}`),
        ])
      : el("div", { className: "grid-kpis", style: "margin-bottom:12px" }, [
          kpiTile("KV Offload Throughput", offBytes != null ? bytesFmt(offBytes) : (offRate != null ? `${num(offRate / (1024 ** 2), 1)} MiB/s` : "N/A"), rateSub(offRate, offPeakRate)),
          kpiTile("KV Restore Throughput", restBytes != null ? bytesFmt(restBytes) : (restRate != null ? `${num(restRate / (1024 ** 2), 1)} MiB/s` : "N/A"), rateSub(restRate, restPeakRate)),
          kpiTile("KV Offload Lookup Sync Delay (P95)", lookupP95Ms != null ? msFmt(lookupP95Ms) : "N/A", "P/D NIXL: N/A (Aggregated)"),
        ]);

    const hasTierUsageChartData = srvPts.some((p, i) =>
      effectiveKvPoint(p, i) !== null
      || isFiniteMetric(p.cpu_cache_usage_percent)
      || isFiniteMetric(p.disk_cache_usage_percent)
    );
    const usageHost = el("div", { className: "chart-box" });
    const usageSeries = hasTierUsageChartData
      ? (isDisaggregated && hasDecodeKv && hasPrefillKv
          ? [
              { label: "Decode VRAM KV Usage (%)", color: PALETTE[0], points: srvPts.map((p, i) => ({ x: p._t, y: decodeKvPoint(p, i) })) },
              { label: "Prefill VRAM KV Usage (%)", color: PALETTE[3], points: srvPts.map((p, i) => ({ x: p._t, y: prefillKvPoint(p, i) })) },
              { label: "CPU KV Cache Usage (%)", color: PALETTE[1], points: srvPts.map((p) => ({ x: p._t, y: clampPct(p.cpu_cache_usage_percent) })) },
              { label: "NVMe KV Cache Usage (%)", color: PALETTE[2], points: srvPts.map((p) => ({ x: p._t, y: clampPct(p.disk_cache_usage_percent) })) },
            ]
          : [
              { label: "VRAM KV Cache Usage (%)", color: PALETTE[0], points: srvPts.map((p) => ({ x: p._t, y: clampPct(isFiniteMetric(p.kv_cache_usage_percent) ? p.kv_cache_usage_percent : p.gpu_cache_usage_percent) })) },
              { label: "CPU KV Cache Usage (%)", color: PALETTE[1], points: srvPts.map((p) => ({ x: p._t, y: clampPct(p.cpu_cache_usage_percent) })) },
              { label: "NVMe KV Cache Usage (%)", color: PALETTE[2], points: srvPts.map((p) => ({ x: p._t, y: clampPct(p.disk_cache_usage_percent) })) },
            ])
      : [];
    LineBandChart(usageHost, {
      yMin: 0,
      yMax: 100,
      yUnit: "%",
      emptyText: "VRAM, CPU, and NVMe KV cache usage gauges were not reported over time.",
      series: usageSeries,
    });

    const hasOffloadThroughputData = srvPts.some((p) =>
      isFiniteMetric(p.kv_offload_bytes_per_second)
      || isFiniteMetric(p.kv_restore_bytes_per_second)
      || (showNixl && isFiniteMetric(p.nixl_transfer_bytes_per_second ?? p.nixl_bytes_per_second))
    );
    const offHost = el("div", { className: "chart-box" });
    LineBandChart(offHost, {
      yUnit: "MiB/s",
      emptyText: showNixl
        ? "KV offload, restore, and NIXL transfer throughput metrics were not detected (feature not enabled for this deployment)."
        : "KV offload and restore throughput metrics were not detected for this deployment.",
      series: hasOffloadThroughputData
        ? [
            { label: "KV Offload (MiB/s)", color: PALETTE[0], points: srvPts.map((p) => ({ x: p._t, y: isFiniteMetric(p.kv_offload_bytes_per_second) ? p.kv_offload_bytes_per_second / (1024 ** 2) : null })) },
            { label: "KV Restore (MiB/s)", color: PALETTE[1], points: srvPts.map((p) => ({ x: p._t, y: isFiniteMetric(p.kv_restore_bytes_per_second) ? p.kv_restore_bytes_per_second / (1024 ** 2) : null })) },
            ...(showNixl ? [{ label: "NIXL KV Transfer (MiB/s)", color: PALETTE[3], points: srvPts.map((p) => { const b = p.nixl_transfer_bytes_per_second ?? p.nixl_bytes_per_second; return { x: p._t, y: isFiniteMetric(b) ? b / (1024 ** 2) : null }; }) }] : []),
          ]
        : [],
    });
    const offloadHeaderBadge = showNixl ? null : el("div", { style: "margin-bottom:8px" }, [el("span", { className: "badge", textContent: "P/D NIXL: N/A (Aggregated)" })]);
    pageNode.appendChild(makeSection(
      "s-offload",
      showNixl ? "KV Offload · Multi-Tier Usage & NIXL Transfer" : "KV Offload · Multi-Tier Usage & Offload",
      showNixl ? "VRAM, CPU, and NVMe KV cache usage alongside offload, restore, and NIXL transfers" : "VRAM, CPU, and NVMe KV cache usage alongside offload and restore throughput (Aggregated topology)",
      el("div", null, [
        offloadHeaderBadge,
        tierUsageKpis,
        offloadKpis,
        el("div", { className: "grid-2" }, [
          chartCard("KV Cache Usage by Tier", usageHost),
          chartCard(showNixl ? "KV Offload, Restore & NIXL Transfer Throughput" : "KV Offload & Restore Throughput", offHost),
        ]),
      ])
    ));

    // 8. #s-evict · KV Eviction (Block Churn, Residency & Preemptions)
    const preemptMax = (summary.vllm_preemptions_total && summary.vllm_preemptions_total.max) ?? (summary.num_preemptions_total && summary.num_preemptions_total.max);
    const preemptMin = summary.vllm_preemptions_total && summary.vllm_preemptions_total.min;
    const totalPreempt = st.total_preemptions ?? (isFiniteMetric(preemptMax) && isFiniteMetric(preemptMin) ? Math.max(0, preemptMax - preemptMin) : preemptMax);
    const preemptRatePeak = (summary.vllm_preemptions_per_second && summary.vllm_preemptions_per_second.max) ?? (summary.preemptions_per_second && summary.preemptions_per_second.max);

    const blockSecFmt = (v) => {
      if (!isFiniteMetric(v)) return "—";
      return fmtDec(Number(v)) + "s";
    };
    const kvBlockStat = (metricBase, pctKey) => {
      const flat = podStatMean(summary, `${metricBase}_${pctKey}_seconds`);
      if (flat !== null) return flat;
      const nested = summary[`${metricBase}_seconds`];
      if (nested && typeof nested === "object" && isFiniteMetric(nested[pctKey])) return Number(nested[pctKey]);
      return null;
    };

    const kvCacheMetricsSample = isFiniteMetric(cacheConfig.kv_cache_metrics_sample) ? Number(cacheConfig.kv_cache_metrics_sample) : null;
    const evictBlocksSummary = summary.kv_blocks_evicted_per_minute;
    const evictBlocksPeak = evictBlocksSummary && isFiniteMetric(evictBlocksSummary.max) ? Number(evictBlocksSummary.max) : null;
    const evictBlocksMean = podStatMean(summary, "kv_blocks_evicted_per_minute");
    const evictBlocksTotal = isFiniteMetric(derived.kv_blocks_evicted_total) ? Number(derived.kv_blocks_evicted_total) : null;

    const idleP50 = kvBlockStat("kv_block_idle_before_evict", "p50");
    const idleP90 = kvBlockStat("kv_block_idle_before_evict", "p90");
    const idleP99 = kvBlockStat("kv_block_idle_before_evict", "p99");
    const lifeP50 = kvBlockStat("kv_block_lifetime", "p50");
    const lifeP90 = kvBlockStat("kv_block_lifetime", "p90");
    const lifeP99 = kvBlockStat("kv_block_lifetime", "p99");
    const reuseP50 = kvBlockStat("kv_block_reuse_gap", "p50");
    const reuseP90 = kvBlockStat("kv_block_reuse_gap", "p90");
    const reuseP99 = kvBlockStat("kv_block_reuse_gap", "p99");

    const hasEvictBlocksVal = evictBlocksPeak !== null || evictBlocksMean !== null;
    const evictBlocksSub = hasEvictBlocksVal || evictBlocksTotal !== null
      ? `Peak / Mean${evictBlocksTotal !== null ? ` · Total: ${intFmt(evictBlocksTotal)} blocks` : ""}${kvCacheMetricsSample !== null && kvCacheMetricsSample < 1.0 ? ` · Sample ${kvCacheMetricsSample}x` : ""}`
      : "--kv-cache-metrics off";

    const evictKpis = el("div", { className: "grid-kpis", style: "margin-bottom:12px" }, [
      kpiTile("Total Preemptions", totalPreempt != null ? intFmt(totalPreempt) : "0", `Peak KV Cache: ${pct(kvPeak)}`),
      kpiTile("Peak Preemption Rate", `${num(preemptRatePeak ?? 0, 3)} /s`, (totalPreempt || 0) > 0 ? "Evicting" : "Nominal"),
      kpiTile("KV Cache Pressure", (kvPeak || 0) >= 90 || (totalPreempt || 0) > 0 ? "Pressure Detected" : "Healthy", "Threshold: 90% HBM / >0 preemptions"),
      kpiTile("KV Block Eviction Rate", hasEvictBlocksVal ? `${num(evictBlocksPeak ?? evictBlocksMean, 1)} / ${num(evictBlocksMean ?? evictBlocksPeak, 1)} blocks/min` : "—", evictBlocksSub),
      kpiTile("KV Block Idle Time Before Eviction (P50 / P90)", idleP50 !== null || idleP90 !== null ? `${blockSecFmt(idleP50)} / ${blockSecFmt(idleP90)}` : "—", idleP99 !== null ? `P99: ${blockSecFmt(idleP99)} · vllm:kv_block_idle_before_evict_seconds` : "vllm:kv_block_idle_before_evict_seconds"),
      kpiTile("KV Block Lifetime (P50 / P90)", lifeP50 !== null || lifeP90 !== null ? `${blockSecFmt(lifeP50)} / ${blockSecFmt(lifeP90)}` : "—", lifeP99 !== null ? `P99: ${blockSecFmt(lifeP99)} · vllm:kv_block_lifetime_seconds` : "vllm:kv_block_lifetime_seconds"),
      kpiTile("KV Block Reuse Gap (P50 / P90)", reuseP50 !== null || reuseP90 !== null ? `${blockSecFmt(reuseP50)} / ${blockSecFmt(reuseP90)}` : "—", reuseP99 !== null ? `P99: ${blockSecFmt(reuseP99)} · vllm:kv_block_reuse_gap_seconds` : "vllm:kv_block_reuse_gap_seconds"),
    ]);

    const evictHost = el("div", { className: "chart-box" });
    LineBandChart(evictHost, {
      emptyText: "vLLM preemption rate time series unavailable.",
      series: srvPts.some((p) => isFiniteMetric(p.vllm_preemptions_per_second ?? p.preemptions_per_second))
        ? [{ label: "Preemptions/s", color: PALETTE[4], points: srvPts.map((p) => ({ x: p._t, y: p.vllm_preemptions_per_second ?? p.preemptions_per_second })) }]
        : [],
    });

    const hasKvEvictBlocksSeries = srvPts.some((p) => isFiniteMetric(p.kv_blocks_evicted_per_minute));
    const hasIdleSeries = srvPts.some((p) => isFiniteMetric(p.kv_block_idle_before_evict_p50_seconds) || isFiniteMetric(p.kv_block_idle_before_evict_p90_seconds));
    const hasLifeSeries = srvPts.some((p) => isFiniteMetric(p.kv_block_lifetime_p50_seconds) || isFiniteMetric(p.kv_block_lifetime_p90_seconds));
    const hasReuseSeries = srvPts.some((p) => isFiniteMetric(p.kv_block_reuse_gap_p50_seconds) || isFiniteMetric(p.kv_block_reuse_gap_p90_seconds));
    const hasKvBlockSeries = hasKvEvictBlocksSeries || hasIdleSeries || hasLifeSeries || hasReuseSeries;

    const evictBodyChildren = [evictKpis];
    if (hasKvBlockSeries) {
      const evictBlocksHost = el("div", { className: "chart-box" });
      const sampleScaled = kvCacheMetricsSample !== null && kvCacheMetricsSample < 1.0;
      const sampleBadge = sampleScaled
        ? el("span", { className: "badge info", textContent: `Sample-scaled (rate ${kvCacheMetricsSample})` })
        : null;
      LineBandChart(evictBlocksHost, {
        emptyText: "KV blocks evicted rate time series unavailable.",
        series: hasKvEvictBlocksSeries
          ? [{
              label: sampleScaled ? `Blocks Evicted/min (scaled 1/${kvCacheMetricsSample})` : "Blocks Evicted/min",
              color: PALETTE[4],
              points: srvPts.map((p) => ({ x: p._t, y: p.kv_blocks_evicted_per_minute })),
            }]
          : [],
      });

      const idleHost = el("div", { className: "chart-box" });
      LineBandChart(idleHost, {
        emptyText: "vllm:kv_block_idle_before_evict_seconds series unavailable.",
        series: hasIdleSeries
          ? [
              { label: "Idle Before Evict P50 (s)", color: PALETTE[0], points: srvPts.map((p) => ({ x: p._t, y: p.kv_block_idle_before_evict_p50_seconds })) },
              { label: "Idle Before Evict P90 (s)", color: PALETTE[2], dashed: true, points: srvPts.map((p) => ({ x: p._t, y: p.kv_block_idle_before_evict_p90_seconds })) },
            ]
          : [],
      });

      const lifeHost = el("div", { className: "chart-box" });
      LineBandChart(lifeHost, {
        emptyText: "vllm:kv_block_lifetime_seconds series unavailable.",
        series: hasLifeSeries
          ? [
              { label: "Block Lifetime P50 (s)", color: PALETTE[1], points: srvPts.map((p) => ({ x: p._t, y: p.kv_block_lifetime_p50_seconds })) },
              { label: "Block Lifetime P90 (s)", color: PALETTE[3], dashed: true, points: srvPts.map((p) => ({ x: p._t, y: p.kv_block_lifetime_p90_seconds })) },
            ]
          : [],
      });

      const reuseHost = el("div", { className: "chart-box" });
      LineBandChart(reuseHost, {
        emptyText: "vllm:kv_block_reuse_gap_seconds series unavailable.",
        series: hasReuseSeries
          ? [
              { label: "Reuse Gap P50 (s)", color: PALETTE[5], points: srvPts.map((p) => ({ x: p._t, y: p.kv_block_reuse_gap_p50_seconds })) },
              { label: "Reuse Gap P90 (s)", color: PALETTE[4], dashed: true, points: srvPts.map((p) => ({ x: p._t, y: p.kv_block_reuse_gap_p90_seconds })) },
            ]
          : [],
      });

      evictBodyChildren.push(
        el("div", { className: "grid-2" }, [
          chartCard("Request Preemption Rate over Time", evictHost),
          chartCard("KV Block Eviction Rate (blocks/min)", evictBlocksHost, null, sampleBadge),
        ]),
        el("div", { className: "grid-2", style: "margin-top:12px" }, [
          chartCard("KV Block Idle Time Before Eviction (s)", idleHost),
          chartCard("KV Block Lifetime (s)", lifeHost),
        ]),
        el("div", { style: "margin-top:12px" }, [
          chartCard("KV Block Reuse Gap (s)", reuseHost),
        ])
      );
    } else {
      const evictBlocksEmpty = el("div", { className: "empty-note" }, [
        el("div", {
          style: "font-weight:600;margin-bottom:4px;color:var(--text);",
          textContent: "KV block eviction & residency metrics were not exposed by this vLLM deployment.",
        }),
        el("div", {
          textContent: "Metrics vllm:kv_block_idle_before_evict_seconds, vllm:kv_block_lifetime_seconds, and vllm:kv_block_reuse_gap_seconds require enabling --kv-cache-metrics (with optional --kv-cache-metrics-sample 0.01) on vLLM.",
        }),
      ]);
      evictBodyChildren.push(
        el("div", { className: "grid-2" }, [
          chartCard("Request Preemption Rate over Time", evictHost),
          chartCard("KV Block Eviction Rate (blocks/min)", evictBlocksEmpty),
        ])
      );
    }

    pageNode.appendChild(
      makeSection(
        "s-evict",
        "KV Eviction · Block Churn, Residency & Preemptions",
        "Accelerator KV block eviction rate, residency lifetimes, and vLLM preemption pressure",
        el("div", null, evictBodyChildren)
      )
    );

    // 9. #s-power · Accelerator Power, Utilization & Memory (Profile-Driven, 2 separate charts)
    const memUsed = podStatMean(summary, "gpu_framebuffer_used_bytes");
    const memTotal = podStatMean(summary, "gpu_memory_total_bytes");
    const pwrKpis = el("div", { className: "grid-kpis", style: "margin-bottom:12px" }, [
      kpiTile(`${hwLabel} Power`, `${num(podStatMean(summary, "gpu_power_watts") ?? energy.accelerator_avg_power_watts, 1)} W`, `Peak: ${num((summary.gpu_power_watts && summary.gpu_power_watts.max) ?? energy.accelerator_peak_power_watts, 1)} W | Energy: ${num(energy.accelerator_energy_wh, 2)} Wh`),
      kpiTile(`${hwLabel} Utilization`, pct(clampPct(podStatMean(summary, "gpu_utilization_percent"))), `Peak: ${pct(clampPct(summary.gpu_utilization_percent && summary.gpu_utilization_percent.max))}`),
      kpiTile(`${hwLabel} Memory`, `${bytesFmt(memUsed)} / ${bytesFmt(memTotal)}`, `Mem BW: ${pct(clampPct(podStatMean(summary, "gpu_memory_bandwidth_utilization_percent")))}`),
      kpiTile(`${hwLabel} Temperature`, `${num(podStatMean(summary, "gpu_temperature_celsius"), 1)} °C`, `Peak: ${num(summary.gpu_temperature_celsius && summary.gpu_temperature_celsius.max, 1)} °C | Efficiency: ${num(energy.output_tokens_per_wh, 1)} tok/Wh`),
    ]);
    const accPwrHost = el("div", { className: "chart-box" });
    const accUtilHost = el("div", { className: "chart-box" });
    LineBandChart(accPwrHost, {
      emptyText: `No ${hwLabel.toLowerCase()} power series available.`,
      series: srvPts.some((p) => isFiniteMetric(p.gpu_power_watts))
        ? [{ label: `${hwLabel} Power (W)`, color: PALETTE[2], points: srvPts.map((p) => ({ x: p._t, y: p.gpu_power_watts })) }]
        : [],
    });
    LineBandChart(accUtilHost, {
      emptyText: `No ${hwLabel.toLowerCase()} utilization series available.`,
      yMin: 0,
      yMax: 100,
      series: srvPts.some((p) => isFiniteMetric(p.gpu_utilization_percent) || isFiniteMetric(p.gpu_memory_bandwidth_utilization_percent ?? p.gpu_memory_utilization_percent))
        ? [
            { label: `${hwLabel} Util %`, color: PALETTE[0], points: srvPts.map((p) => ({ x: p._t, y: clampPct(p.gpu_utilization_percent) })) },
            { label: "Mem Bandwidth %", color: PALETTE[1], dashed: true, points: srvPts.map((p) => ({ x: p._t, y: clampPct(p.gpu_memory_bandwidth_utilization_percent ?? p.gpu_memory_utilization_percent) })) },
          ]
        : [],
    });
    const srcKeys = Object.keys(metricSources);
    const devDetails = allocatedDevices.length || srcKeys.length
      ? el("details", { className: "collapsible" }, [
          el("summary", { textContent: `Telemetry Sources & Allocated Devices (${allocatedDevices.length} devices, ${srcKeys.length} sources)` }),
          allocatedDevices.length
            ? makeSortableTable(
                [
                  { key: "pod", label: "Pod" },
                  { key: "node", label: "Node" },
                  { key: "pci_bdf", label: "PCI BDF / Device", getter: (r) => r.pci_bdf || r.device },
                ],
                allocatedDevices
              )
            : null,
          srcKeys.length
            ? el(
                "div",
                { style: "margin-top:8px" },
                [
                  makeSortableTable(
                    [
                      { key: "metric", label: "Metric" },
                      { key: "source", label: "Telemetry Source" },
                    ],
                    srcKeys.map((k) => ({ metric: k, source: String(metricSources[k]) }))
                  ),
                ]
              )
            : null,
        ])
      : null;
    pageNode.appendChild(
      makeSection(
        "s-power",
        `${hwLabel} Power, Utilization & Memory`,
        "Hardware-profile driven device telemetry",
        el("div", null, [
          pwrKpis,
          el("div", { className: "grid-2" }, [
            chartCard(`${hwLabel} Power Draw over Time`, accPwrHost),
            chartCard(`${hwLabel} Compute & Memory Bandwidth Utilization`, accUtilHost),
          ]),
          devDetails,
        ])
      )
    );

    // 10. #s-grafana · Grafana Deep Links
    const links = (d.grafana_links || []).filter((l) => l && isSafeHttpUrl(l.url));
    const linkBody = links.length
      ? el(
          "div",
          { className: "link-row" },
          links.map((l) => el("a", { className: "link-btn", href: l.url, target: "_blank", rel: "noopener noreferrer", textContent: `${l.label || l.title || "Grafana Dashboard"} ↗` }))
        )
      : el("div", { className: "empty-note", textContent: "Grafana URL is not configured for the target cluster." });
    pageNode.appendChild(makeSection("s-grafana", "Grafana Deep Links", "Cluster Grafana dashboards scoped to this simulation run", linkBody));
  }

  function renderRequests(pageNode) {
    const d = state.data;
    const cs = d.client_series || {};
    const buckets = cs.buckets || cs.timeseries || [];
    const fidelityBuckets = Array.isArray(cs.fidelity_series) && cs.fidelity_series.length ? cs.fidelity_series : buckets;
    const fullWin = (cs.windows && cs.windows.full) || {};
    const dispatchDuration = isFiniteMetric(cs.dispatch_duration_seconds) ? Number(cs.dispatch_duration_seconds) : null;
    const fullDuration = isFiniteMetric(cs.duration_seconds)
      ? Number(cs.duration_seconds)
      : (isFiniteMetric(fullWin.duration_seconds) ? Number(fullWin.duration_seconds) : null);
    const hasGraceDrain = dispatchDuration !== null && fullDuration !== null && (fullDuration - dispatchDuration) >= 3.0;
    const fidSubtitle = hasGraceDrain
      ? `Send window: 0–${fmtDec(dispatchDuration)}s · Grace drain: ${fmtDec(dispatchDuration)}–${fmtDec(fullDuration)}s (in-flight completion)`
      : "Target arrival rate vs actual send rate and schedule drift (ms)";
    const defaultBucketSec = isFiniteMetric(cs.bucket_seconds) ? Number(cs.bucket_seconds) : null;
    const bucketX = (p) => Number(p.mid_seconds ?? p.time_seconds ?? p.start_seconds ?? p.t ?? 0);
    const bucketActualSendRps = (p) => {
      if (isFiniteMetric(p.actual_send_rps)) return Number(p.actual_send_rps);
      if (isFiniteMetric(p.arrived_rps)) return Number(p.arrived_rps);
      const dur = isFiniteMetric(p.end_seconds) && isFiniteMetric(p.start_seconds) ? Number(p.end_seconds) - Number(p.start_seconds) : defaultBucketSec;
      if (isFiniteMetric(p.arrived_requests) && dur && dur > 0) return Number(p.arrived_requests) / dur;
      return p.throughput_rps ?? p.completed_rps;
    };

    const fidHost = el("div", { className: "chart-box" });
    pageNode.appendChild(makeSection("r-fidelity", "Trace Replay Fidelity & Schedule Drift", fidSubtitle, fidHost));
    LineBandChart(fidHost, {
      minX: fidelityBuckets.length && dispatchDuration && dispatchDuration > 0 ? 0 : undefined,
      maxX: fidelityBuckets.length && dispatchDuration && dispatchDuration > 0 ? dispatchDuration : undefined,
      rightLabel: "ms",
      series: [
        { label: "Target Arrival (req/s)", dashed: true, points: fidelityBuckets.map((p) => ({ x: bucketX(p), y: p.target_arrival_rps ?? p.target_rps })) },
        { label: "Actual Send (req/s)", points: fidelityBuckets.map((p) => ({ x: bucketX(p), y: bucketActualSendRps(p) })) },
        { label: "Schedule Drift P90 (ms)", axis: "right", points: fidelityBuckets.map((p) => ({ x: bucketX(p), y: statVal(p.drift_ms, "p90") ?? p.drift_p90_ms })) },
      ],
    });

    const driftHist = Array.isArray(cs.drift_histogram) ? cs.drift_histogram : (Array.isArray(cs.s_time_drift_histogram) ? cs.s_time_drift_histogram : []);
    if (driftHist.length) {
      const dhHost = el("div", { className: "chart-box" });
      pageNode.appendChild(makeSection("r-drift-hist", "Schedule Drift Distribution", "Request count histogram by schedule drift bucket (ms)", dhHost));
      StackedBarAreaChart(dhHost, {
        grouped: true,
        items: driftHist.map((b, idx) => ({
          label: String(b.label ?? b.bucket ?? b.bin ?? (b.min_ms != null && b.max_ms != null ? `${b.min_ms}-${b.max_ms}ms` : idx)),
          segments: [{ label: "Requests", value: b.count ?? b.requests ?? b.value ?? 0, color: PALETTE[2] }],
        })),
      });
    }

    const warmupSeconds = Math.max(0, Number((cs.warmup_seconds ?? (d.task && d.task.warmup_seconds)) || 0) || 0);
    const lanesSubtitle = "One row per concurrent session slot · Requests packed into slots · a slot is reused once its last request finished"
      + (warmupSeconds > 0 ? " · shaded span = warmup" : "");
    const lanesHost = el("div", { style: "width:100%;min-width:0;" });
    pageNode.appendChild(makeSection("r-lanes", "Sessions over Time", lanesSubtitle, lanesHost));
    SwimlaneCanvas(lanesHost, cs, d.sessions_summary, warmupSeconds);

    const bHost = el("div", { className: "grid-2" });
    const promptBar = el("div", { className: "chart-box" });
    const cacheBar = el("div", { className: "chart-box" });
    const hmTtftBox = el("div", { className: "chart-box" });
    const hmTpotBox = el("div", { className: "chart-box" });
    bHost.append(
      chartCard("TTFT by Input Sequence Length Bucket", promptBar),
      chartCard("TTFT by Prefix Cache Hit Bucket", cacheBar),
      chartCard("TTFT by Time and Input Sequence Length", hmTtftBox),
      chartCard("TPOT by Time and Output Sequence Length", hmTpotBox)
    );
    pageNode.appendChild(makeSection("r-buckets", "Latency by Input Sequence Length, Cache-Hit & Request Scatter", "Input sequence length / prefix cache hit buckets & per-request time × sequence-length latency scatter", bHost));
    StackedBarAreaChart(promptBar, {
      grouped: true,
      emptyText: "Input sequence length buckets unavailable.",
      items: (cs.prompt_length_buckets || cs.prompt_buckets || []).map((b) => ({
        label: b.label || b.bucket,
        segments: [
          { label: "TTFT P50 (ms)", value: (b.ttft_ms && b.ttft_ms.p50) ?? b.ttft_p50_ms ?? b.p50_ms ?? 0, color: PALETTE[0], extra: `n=${b.count || 0}` },
          { label: "TTFT P90 (ms)", value: (b.ttft_ms && b.ttft_ms.p90) ?? b.ttft_p90_ms ?? b.p90_ms ?? 0, color: PALETTE[2], extra: `n=${b.count || 0}` },
        ],
      })),
    });
    const ss = d.sessions_summary || {};
    StackedBarAreaChart(cacheBar, {
      grouped: true,
      emptyText: "Prefix cache hit buckets unavailable.",
      items: (ss.cache_buckets || cs.cache_hit_buckets || cs.cache_buckets || []).map((b) => ({
        label: b.label || b.bucket,
        segments: [
          { label: "TTFT P50 (ms)", value: (b.ttft_ms && b.ttft_ms.p50) ?? b.ttft_p50_ms ?? b.p50_ms ?? 0, color: PALETTE[0], extra: `n=${b.count || 0}` },
          { label: "TTFT P90 (ms)", value: (b.ttft_ms && b.ttft_ms.p90) ?? b.ttft_p90_ms ?? b.p90_ms ?? 0, color: PALETTE[2], extra: `n=${b.count || 0}` },
        ],
      })),
    });
    const win = getActiveWindowStats();
    const hasWindowHeatmap = Boolean(win && win.heatmap && typeof win.heatmap === "object");
    const ttftHeatmap = hasWindowHeatmap
      ? win.heatmap.ttft
      : (cs.heatmap && cs.heatmap.ttft) || cs.ttft_heatmap || (cs.heatmaps && cs.heatmaps.ttft) || null;
    const tpotHeatmap = hasWindowHeatmap
      ? win.heatmap.tpot
      : (cs.heatmap && cs.heatmap.tpot) || cs.tpot_heatmap || (cs.heatmaps && cs.heatmaps.tpot) || null;
    HeatmapCanvas(hmTtftBox, ttftHeatmap, "TTFT");
    HeatmapCanvas(hmTpotBox, tpotHeatmap, "TPOT");
    const convHost = el("div", { className: "grid-2" });
    const turnsBox = el("div", { className: "chart-box" });
    const depthBox = el("div", { className: "chart-box" });
    convHost.append(
      chartCard("Turns per Session", turnsBox),
      chartCard("Conversation Tree Depth", depthBox)
    );
    const turns = ss.turn_distribution || ss.turn_histogram || [];
    const depths = ss.tree_depth_distribution || [];
    pageNode.appendChild(makeSection("r-conv", "Conversation Structure", ss.available ? `Matched ${intFmt(ss.correlation && ss.correlation.matched_count)} / ${intFmt(ss.correlation && ss.correlation.total_count)}` : "Session structure", convHost));
    StackedBarAreaChart(turnsBox, {
      grouped: true,
      xLabel: "Turns per session",
      yLabel: "Sessions",
      emptyText: ss.reason || "Session identifiers are not present in this trace format.",
      items: turns.map((b) => {
        const rawTurns = b.turns ?? b.turn ?? b.label;
        const rawText = String(rawTurns ?? "").trim();
        const turnNum = rawText === "" ? NaN : Number(rawText);
        const turnLabel = !rawText
          ? ""
          : Number.isFinite(turnNum)
            ? (turnNum === 1 ? "1 turn" : `${turnNum} turns`)
            : /\bturns?\b/i.test(rawText)
              ? rawText
              : `${rawText} turns`;
        return {
          label: turnLabel,
          segments: [{ label: "Sessions", value: b.sessions ?? b.count ?? 0, color: PALETTE[3] }],
        };
      }),
    });
    StackedBarAreaChart(depthBox, {
      grouped: true,
      emptyText: ss.reason || "Conversation tree depth unavailable.",
      items: depths.map((b) => ({
        label: `depth ${b.depth ?? b.label}`,
        segments: [{ label: "Sessions", value: b.sessions ?? b.count ?? 0, color: PALETTE[1] }],
      })),
    });

    const cdfHost = el("div");
    const rows = [
      { name: "TTFT", dist: win.ttft_ms || win.ttft },
      { name: "TPOT", dist: win.tpot_ms || win.tpot },
      { name: "E2E", dist: win.e2e_ms || win.e2e },
      { name: "Schedule Drift", dist: win.drift_ms || win.drift },
    ]
      .filter((r) => r.dist)
      .map((r) => ({
        metric: r.name,
        mean_ms: statVal(r.dist, "mean_ms"),
        min_ms: statVal(r.dist, "min_ms"),
        p50_ms: statVal(r.dist, "p50_ms"),
        p90_ms: statVal(r.dist, "p90_ms"),
        p95_ms: statVal(r.dist, "p95_ms"),
        p99_ms: statVal(r.dist, "p99_ms"),
        max_ms: statVal(r.dist, "max_ms"),
      }));
    const fmtRowMetric = (v, row) => (row && row.metric === "TPOT" ? tpotFmt(v) : msFmt(v));
    const pctTable = rows.length
      ? makeSortableTable(
          [
            {
              key: "metric",
              label: "Metric",
              render: (v) => el("span", { className: "th-inner" }, [el("span", { textContent: v }), helpBadge(v)]),
            },
            { key: "mean_ms", label: "Avg", num: true, format: fmtRowMetric },
            { key: "min_ms", label: "Min", num: true, format: fmtRowMetric },
            { key: "p50_ms", label: "P50", num: true, format: fmtRowMetric },
            { key: "p90_ms", label: "P90", num: true, format: fmtRowMetric },
            { key: "p95_ms", label: "P95", num: true, format: fmtRowMetric },
            { key: "p99_ms", label: "P99", num: true, format: fmtRowMetric },
            { key: "max_ms", label: "Max", num: true, format: fmtRowMetric },
          ],
          rows
        )
      : el("div", { className: "empty-note", textContent: "Percentile summary unavailable." });
    const cdfWrap = el("div", null, [cdfHost, el("div", { style: "margin-top:12px" }, [pctTable])]);
    pageNode.appendChild(makeSection("r-cdf", "CDF & Full Percentile Table", `Active window: ${state.windowMode}`, cdfWrap));
    CdfChart(cdfHost, cs.cdf || {}, (d.forensics && d.forensics.verdict && d.forensics.verdict.slo) || {});

    const errBlock = el("div");
    const counts = d.error_counts || {};
    const samples = d.error_samples || [];
    const pills = el(
      "div",
      { className: "filter-bar" },
      Object.entries(counts).map(([code, cnt]) =>
        el("button", {
          type: "button",
          className: `pill-btn${state.errorFilter === code ? " active" : ""}`,
          textContent: `${code} (${cnt})`,
          onClick: () => {
            state.errorFilter = code;
            renderErrorTable();
            for (const b of pills.querySelectorAll("button")) {
              b.classList.toggle("active", b.textContent.startsWith(code + " "));
            }
          },
        })
      )
    );
    const errTableHost = el("div");
    function renderErrorTable() {
      const filtered = samples.filter((s) => {
        if (state.errorFilter === "all") return true;
        return String(s.status_code) === state.errorFilter || s.category === state.errorFilter;
      });
      if (!filtered.length) {
        errTableHost.replaceChildren(el("div", { className: "empty-note", textContent: "No errors recorded for this filter." }));
        return;
      }
      errTableHost.replaceChildren(
        makeSortableTable(
          [
            { key: "request_number", label: "Req #", num: true },
            { key: "started_at_seconds", label: "Time (s)", num: true, format: (v) => num(v, 2) },
            { key: "category", label: "Category" },
            { key: "status_code", label: "Code", num: true },
            { key: "latency_ms", label: "Latency", num: true, format: msFmt },
            { key: "error", label: "Error Message (Redacted)" },
          ],
          filtered
        )
      );
    }
    renderErrorTable();
    errBlock.append(pills, errTableHost);
    pageNode.appendChild(makeSection("r-errors", "Error Breakdown & Samples", "Up to 200 representative redacted failure records", errBlock));
  }

  const BYTES_PER_MIB = 1048576;
  const BYTES_PER_GIB = 1073741824;
  const BY_NIC_SCOPE = "__by_nic__";

  function clampNonNegMetric(v) {
    return isFiniteMetric(v) ? Math.max(0, Number(v)) : null;
  }

  function hasPointNicMap(pt) {
    return Boolean(pt && pt.network_interfaces && typeof pt.network_interfaces === "object" && !Array.isArray(pt.network_interfaces) && Object.keys(pt.network_interfaces).length > 0);
  }

  function resolveActiveNetPoints(selectedNodeName, activeHostSeries, hostSeries, nodeSeriesMap) {
    if (selectedNodeName) return activeHostSeries;
    if (hostSeries.some(hasPointNicMap)) return hostSeries;
    const nodeBuckets = Object.values(nodeSeriesMap || {});
    if (!nodeBuckets.some((pts) => Array.isArray(pts) && pts.some(hasPointNicMap))) {
      return hostSeries;
    }
    const byTime = new Map();
    for (const pts of nodeBuckets) {
      if (!Array.isArray(pts)) continue;
      for (const pt of pts) {
        if (!hasPointNicMap(pt)) continue;
        const x = pt._t ?? pt.t;
        if (!isFiniteMetric(x)) continue;
        const key = Number(x);
        let acc = byTime.get(key);
        if (!acc) {
          acc = Object.create(null);
          byTime.set(key, acc);
        }
        for (const rawDev of Object.keys(pt.network_interfaces)) {
          const dev = String(rawDev || "").trim();
          if (!dev || !Object.prototype.hasOwnProperty.call(pt.network_interfaces, rawDev)) continue;
          const vals = pt.network_interfaces[rawDev];
          if (!vals || typeof vals !== "object") continue;
          const prev = Object.prototype.hasOwnProperty.call(acc, dev)
            ? acc[dev]
            : { rx: 0, tx: 0, hasRx: false, hasTx: false };
          if (isFiniteMetric(vals.rx)) {
            prev.rx += Math.max(0, Number(vals.rx));
            prev.hasRx = true;
          }
          if (isFiniteMetric(vals.tx)) {
            prev.tx += Math.max(0, Number(vals.tx));
            prev.hasTx = true;
          }
          if (prev.hasRx || prev.hasTx) acc[dev] = prev;
        }
      }
    }
    if (!byTime.size) return hostSeries;
    const finalizedFor = (acc) => {
      const out = Object.create(null);
      for (const dev of Object.keys(acc)) {
        const stVals = Object.prototype.hasOwnProperty.call(acc, dev) ? acc[dev] : null;
        if (!stVals) continue;
        const entry = {};
        if (stVals.hasRx) entry.rx = stVals.rx;
        if (stVals.hasTx) entry.tx = stVals.tx;
        if (entry.rx !== undefined || entry.tx !== undefined) out[dev] = entry;
      }
      return out;
    };
    if (hostSeries.length) {
      return hostSeries.map((pt) => {
        const x = pt && (pt._t ?? pt.t);
        const acc = isFiniteMetric(x) ? byTime.get(Number(x)) : null;
        return acc ? { ...pt, network_interfaces: finalizedFor(acc) } : pt;
      });
    }
    return Array.from(byTime.entries())
      .sort((a, b) => a[0] - b[0])
      .map(([x, acc]) => ({ _t: x, network_interfaces: finalizedFor(acc) }));
  }

  function collectAvailableNics(selectedNodeName, selectedNodeObj, sys, perNode, activeNetPoints) {
    const statsByDev = new Map();
    const recordSummaryStat = (devRaw, rxMean, txMean, rxMax, txMax) => {
      const dev = typeof devRaw === "string" ? devRaw.trim() : "";
      if (!dev) return;
      const cur = statsByDev.get(dev) || { mean: 0, max: 0, fromSummary: false };
      cur.mean += (isFiniteMetric(rxMean) ? Math.max(0, Number(rxMean)) : 0) + (isFiniteMetric(txMean) ? Math.max(0, Number(txMean)) : 0);
      cur.max += (isFiniteMetric(rxMax) ? Math.max(0, Number(rxMax)) : 0) + (isFiniteMetric(txMax) ? Math.max(0, Number(txMax)) : 0);
      cur.fromSummary = true;
      statsByDev.set(dev, cur);
    };

    if (selectedNodeName) {
      const nodeNics = Array.isArray(selectedNodeObj && selectedNodeObj.network_interfaces) ? selectedNodeObj.network_interfaces : [];
      for (const item of nodeNics) {
        if (!item || typeof item !== "object") continue;
        recordSummaryStat(
          item.device,
          podStatMean(item, "rx_bytes_per_second"),
          podStatMean(item, "tx_bytes_per_second"),
          statMax(item.rx_bytes_per_second),
          statMax(item.tx_bytes_per_second)
        );
      }
    } else if (Array.isArray(sys && sys.network_interfaces) && sys.network_interfaces.length) {
      for (const item of sys.network_interfaces) {
        if (!item || typeof item !== "object") continue;
        recordSummaryStat(
          item.device,
          podStatMean(item, "rx_bytes_per_second"),
          podStatMean(item, "tx_bytes_per_second"),
          statMax(item.rx_bytes_per_second),
          statMax(item.tx_bytes_per_second)
        );
      }
    } else {
      for (const nodeItem of perNode || []) {
        const nodeNics = Array.isArray(nodeItem && nodeItem.network_interfaces) ? nodeItem.network_interfaces : [];
        for (const item of nodeNics) {
          if (!item || typeof item !== "object") continue;
          recordSummaryStat(
            item.device,
            podStatMean(item, "rx_bytes_per_second"),
            podStatMean(item, "tx_bytes_per_second"),
            statMax(item.rx_bytes_per_second),
            statMax(item.tx_bytes_per_second)
          );
        }
      }
    }

    const ptAccum = new Map();
    for (const pt of activeNetPoints || []) {
      if (!hasPointNicMap(pt)) continue;
      for (const [rawDev, vals] of Object.entries(pt.network_interfaces)) {
        const dev = String(rawDev || "").trim();
        if (!dev || !vals || typeof vals !== "object") continue;
        const rx = isFiniteMetric(vals.rx) ? Math.max(0, Number(vals.rx)) : 0;
        const tx = isFiniteMetric(vals.tx) ? Math.max(0, Number(vals.tx)) : 0;
        if (!isFiniteMetric(vals.rx) && !isFiniteMetric(vals.tx)) continue;
        const sumVal = rx + tx;
        const acc = ptAccum.get(dev) || { sum: 0, count: 0, max: 0 };
        acc.sum += sumVal;
        acc.count += 1;
        if (sumVal > acc.max) acc.max = sumVal;
        ptAccum.set(dev, acc);
      }
    }
    for (const [dev, acc] of ptAccum.entries()) {
      if (!statsByDev.has(dev)) {
        statsByDev.set(dev, { mean: acc.count > 0 ? acc.sum / acc.count : 0, max: acc.max, fromSummary: false });
      }
    }

    return Array.from(statsByDev.entries())
      .sort((a, b) => (b[1].mean - a[1].mean) || (b[1].max - a[1].max) || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
      .map(([dev]) => dev);
  }

  function buildNicDirectionPoints(points, devices) {
    const rxPts = [];
    const txPts = [];
    for (const pt of points || []) {
      const x = pt._t ?? pt.t;
      const nicMap = pt && pt.network_interfaces && typeof pt.network_interfaces === "object" ? pt.network_interfaces : null;
      let rxSum = 0;
      let hasRx = false;
      let txSum = 0;
      let hasTx = false;
      if (nicMap) {
        for (const dev of devices) {
          const entry = Object.prototype.hasOwnProperty.call(nicMap, dev) ? nicMap[dev] : null;
          if (!entry || typeof entry !== "object") continue;
          if (isFiniteMetric(entry.rx)) {
            rxSum += Math.max(0, Number(entry.rx));
            hasRx = true;
          }
          if (isFiniteMetric(entry.tx)) {
            txSum += Math.max(0, Number(entry.tx));
            hasTx = true;
          }
        }
      }
      rxPts.push({ x, y: hasRx ? rxSum : null });
      txPts.push({ x, y: hasTx ? txSum : null });
    }
    return { rxPts, txPts };
  }

  function renderSystem(pageNode) {
    cleanupChartMountsIn(pageNode);
    if (Array.isArray(pageNode._systemMounts) && pageNode._systemMounts.length) {
      for (const m of pageNode._systemMounts) {
        if (m && typeof m.cleanup === "function") m.cleanup();
      }
      pageNode._systemMounts = [];
    }
    if (Array.isArray(pageNode._systemSubs) && pageNode._systemSubs.length) {
      const staleSubs = new Set(pageNode._systemSubs);
      state.crosshairSubscribers = state.crosshairSubscribers.filter((fn) => !staleSubs.has(fn));
    }
    const subStart = state.crosshairSubscribers.length;

    const st = state.data.server_telemetry || {};
    const cs = state.data.client_series || {};
    const sys = st.system || st.host_metrics || {};
    const summary = st.summary || {};
    const energy = st.energy || {};
    const accelLabel = getHwLabel(st);
    const perNode = Array.isArray(st.per_node) ? st.per_node : [];
    const perNodeSeriesMap =
      st.per_node_series && typeof st.per_node_series === "object" && !Array.isArray(st.per_node_series)
        ? st.per_node_series
        : Object.create(null);

    const nodeNames = [];
    const seenNodeNames = new Set();
    const pushNodeName = (rawName) => {
      const name = typeof rawName === "string" ? rawName.trim() : "";
      if (name && !seenNodeNames.has(name)) {
        seenNodeNames.add(name);
        nodeNames.push(name);
      }
    };
    perNode.forEach((n) => pushNodeName(n && n.node));
    if (Array.isArray(sys.nodes)) sys.nodes.forEach(pushNodeName);
    Object.keys(perNodeSeriesMap).forEach(pushNodeName);

    const selectedNodeName =
      state.systemNode && seenNodeNames.has(state.systemNode)
        ? state.systemNode
        : null;
    const selectedNodeObj = selectedNodeName
      ? perNode.find((n) => n && n.node === selectedNodeName) || { node: selectedNodeName }
      : null;

    function selectSystemNode(nextNode) {
      state.systemNode = nextNode || null;
      pageNode.replaceChildren();
      renderSystem(pageNode);
    }

    function selectSystemNic(nextNic) {
      state.systemNic = nextNic || null;
      pageNode.replaceChildren();
      renderSystem(pageNode);
    }

    function renderNodeScopeControl(ariaLabel) {
      if (nodeNames.length > 5) {
        return makeSelect(
          {
            className: "input",
            "aria-label": ariaLabel,
            onChange: (e) => selectSystemNode(e.target.value === "__all__" ? null : e.target.value),
          },
          [
            { value: "__all__", label: "All Nodes (Aggregate)" },
            ...nodeNames.map((n) => ({ value: n, label: n })),
          ],
          selectedNodeName || "__all__"
        );
      }
      return el("div", { className: "filter-bar", style: "margin-bottom:0" }, [
        el("button", {
          type: "button",
          className: `pill-btn${!selectedNodeName ? " active" : ""}`,
          textContent: "All Nodes (Aggregate)",
          onClick: () => selectSystemNode(null),
        }),
        ...nodeNames.map((n) =>
          el("button", {
            type: "button",
            className: `pill-btn${selectedNodeName === n ? " active" : ""}`,
            textContent: n,
            onClick: () => selectSystemNode(n),
          })
        ),
      ]);
    }

    const srvPts = serverSeriesPoints(st, cs);
    const nodeSeriesMap = Object.create(null);
    for (const name of nodeNames) {
      if (
        Object.prototype.hasOwnProperty.call(perNodeSeriesMap, name) &&
        Array.isArray(perNodeSeriesMap[name]) &&
        perNodeSeriesMap[name].length
      ) {
        nodeSeriesMap[name] = timedSeriesPoints(perNodeSeriesMap[name], st, cs);
      }
    }
    const rawHostSeries = Array.isArray(sys.series) && sys.series.length ? timedSeriesPoints(sys.series, st, cs) : srvPts;
    const hostSeries =
      !rawHostSeries.length && nodeNames.length === 1 && Object.prototype.hasOwnProperty.call(nodeSeriesMap, nodeNames[0])
        ? nodeSeriesMap[nodeNames[0]]
        : rawHostSeries;
    const activeHostSeries =
      selectedNodeName && Object.prototype.hasOwnProperty.call(nodeSeriesMap, selectedNodeName)
        ? nodeSeriesMap[selectedNodeName]
        : selectedNodeName
          ? []
          : hostSeries;
    const activeAccelSeries =
      selectedNodeName && Object.prototype.hasOwnProperty.call(nodeSeriesMap, selectedNodeName)
        ? nodeSeriesMap[selectedNodeName]
        : selectedNodeName
          ? []
          : srvPts;

    const HOST_SERIES_METRIC_KEYS = [
      "cpu_usage_percent",
      "host_cpu_usage_percent",
      "host_cpu_percent",
      "cpu_pct",
      "memory_available_bytes",
      "host_memory_available_bytes",
      "host_mem_available_gb",
      "host_memory_available_gb",
      "mem_available_gb",
      "memory_used_bytes",
      "host_memory_used_bytes",
      "host_mem_used_gb",
      "host_memory_used_gb",
      "mem_used_gb",
      "disk_read_bytes_per_second",
      "host_disk_read_bytes_per_second",
      "host_disk_read_mbps",
      "host_disk_read_mb_s",
      "disk_read_mb_s",
      "disk_written_bytes_per_second",
      "host_disk_written_bytes_per_second",
      "host_disk_write_bytes_per_second",
      "disk_write_bytes_per_second",
      "host_disk_write_mbps",
      "host_disk_write_mb_s",
      "disk_write_mb_s",
      "network_receive_bytes_per_second",
      "host_network_receive_bytes_per_second",
      "host_network_receive_mbps",
      "network_receive_mbps",
      "network_transmit_bytes_per_second",
      "host_network_transmit_bytes_per_second",
      "host_network_transmit_mbps",
      "network_transmit_mbps",
      "host_power_watts",
      "host_power_w",
    ];
    const hasSeriesHostMetrics = (pts) =>
      pts.some((p) => p && (HOST_SERIES_METRIC_KEYS.some((k) => isFiniteMetric(p[k])) || hasPointNicMap(p)));

    const hasSys =
      Boolean(sys.available) ||
      perNode.length > 0 ||
      hasSeriesHostMetrics(activeHostSeries) ||
      Object.values(nodeSeriesMap).some(hasSeriesHostMetrics);
    const note = sys.reason || "Host telemetry unavailable (node_exporter / host power exporter not detected or restricted).";

    const cpuAvg = selectedNodeObj
      ? clampPct(podStatMean(selectedNodeObj, "cpu_usage_percent"))
      : clampPct(firstFinite(sys.cpu_avg_pct, podStatMean(summary, "host_cpu_usage_percent"), podStatMean(summary, "host_cpu_percent")));
    const cpuPeak = selectedNodeObj
      ? clampPct(statMax(selectedNodeObj.cpu_usage_percent))
      : clampPct(firstFinite(sys.cpu_peak_pct, statMax(summary.host_cpu_usage_percent), statMax(summary.host_cpu_percent)));

    const memTotalBytes = selectedNodeObj
      ? firstFinite(selectedNodeObj.memory_total_bytes)
      : firstFinite(sys.mem_total_bytes, podStatMean(summary, "host_memory_total_bytes"));
    const memAvailBytes = selectedNodeObj
      ? podStatMean(selectedNodeObj, "memory_available_bytes")
      : firstFinite(sys.mem_avail_bytes, podStatMean(summary, "host_memory_available_bytes"));
    const memAvailMinBytes = selectedNodeObj
      ? statMin(selectedNodeObj.memory_available_bytes)
      : firstFinite(sys.mem_avail_min_bytes, statMin(summary.host_memory_available_bytes));
    const memUsedBytes = selectedNodeObj
      ? firstFinite(
          podStatMean(selectedNodeObj, "memory_used_bytes"),
          memTotalBytes !== null && memAvailBytes !== null ? Math.max(0, memTotalBytes - memAvailBytes) : null
        )
      : firstFinite(
          sys.mem_used_bytes,
          podStatMean(summary, "host_memory_used_bytes"),
          memTotalBytes !== null && memAvailBytes !== null ? Math.max(0, memTotalBytes - memAvailBytes) : null
        );
    const memPeakPct = selectedNodeObj
      ? clampPct(statMax(selectedNodeObj.memory_usage_percent))
      : clampPct(firstFinite(sys.mem_peak_pct, statMax(summary.host_memory_usage_percent)));

    const diskReadBps = selectedNodeObj
      ? podStatMean(selectedNodeObj, "disk_read_bytes_per_second")
      : firstFinite(sys.disk_read_bps, podStatMean(summary, "host_disk_read_bytes_per_second"));
    const diskWriteBps = selectedNodeObj
      ? podStatMean(selectedNodeObj, "disk_written_bytes_per_second")
      : firstFinite(sys.disk_write_bps, podStatMean(summary, "host_disk_written_bytes_per_second"));
    const diskAvailBytes = selectedNodeObj
      ? firstFinite(selectedNodeObj.filesystem_avail_bytes)
      : firstFinite(sys.disk_avail_bytes, podStatMean(summary, "host_filesystem_avail_bytes"), statMin(summary.host_filesystem_avail_bytes));
    const diskTotalBytes = selectedNodeObj
      ? firstFinite(selectedNodeObj.filesystem_size_bytes)
      : firstFinite(sys.disk_total_bytes, podStatMean(summary, "host_filesystem_size_bytes"));

    const netRxBps = selectedNodeObj
      ? podStatMean(selectedNodeObj, "network_receive_bytes_per_second")
      : firstFinite(
          sys.net_rx_bps,
          podStatMean(summary, "host_network_receive_bytes_per_second"),
          podStatMean(summary, "network_receive_bytes_per_second")
        );
    const netRxPeakBps = selectedNodeObj
      ? statMax(selectedNodeObj.network_receive_bytes_per_second)
      : firstFinite(
          sys.net_rx_peak_bps,
          statMax(summary.host_network_receive_bytes_per_second),
          statMax(summary.network_receive_bytes_per_second)
        );
    const netTxBps = selectedNodeObj
      ? podStatMean(selectedNodeObj, "network_transmit_bytes_per_second")
      : firstFinite(
          sys.net_tx_bps,
          podStatMean(summary, "host_network_transmit_bytes_per_second"),
          podStatMean(summary, "network_transmit_bytes_per_second")
        );
    const netTxPeakBps = selectedNodeObj
      ? statMax(selectedNodeObj.network_transmit_bytes_per_second)
      : firstFinite(
          sys.net_tx_peak_bps,
          statMax(summary.host_network_transmit_bytes_per_second),
          statMax(summary.network_transmit_bytes_per_second)
        );

    const hostPowerAvg = selectedNodeObj
      ? podStatMean(selectedNodeObj, "host_power_watts")
      : firstFinite(sys.host_power_watts, energy.host_avg_power_watts, podStatMean(summary, "host_power_watts"));
    const hostPowerPeak = selectedNodeObj
      ? statMax(selectedNodeObj.host_power_watts)
      : firstFinite(sys.host_peak_power_watts, energy.host_peak_power_watts, statMax(summary.host_power_watts));
    const hostEnergyWh = selectedNodeObj ? null : firstFinite(energy.host_energy_wh, sys.host_energy_wh);

    const accPowerAvg = selectedNodeObj
      ? podStatMean(selectedNodeObj, "gpu_power_watts")
      : firstFinite(podStatMean(summary, "gpu_power_watts"), energy.accelerator_avg_power_watts);
    const accPowerPeak = selectedNodeObj
      ? statMax(selectedNodeObj.gpu_power_watts)
      : firstFinite(statMax(summary.gpu_power_watts), energy.accelerator_peak_power_watts);
    const accTempPeak = selectedNodeObj
      ? firstFinite(statMax(selectedNodeObj.gpu_temperature_celsius), podStatMean(selectedNodeObj, "gpu_temperature_celsius"))
      : firstFinite(statMax(summary.gpu_temperature_celsius), podStatMean(summary, "gpu_temperature_celsius"));

    let scopeRow = null;
    if (nodeNames.length > 0) {
      const targetNodesRow = el("div", { className: "filter-bar", style: "margin-bottom:6px" }, [
        el("span", { className: "kpi-label", textContent: "Target Nodes:" }),
        ...nodeNames.map((n) =>
          el("button", {
            type: "button",
            className: `pill-btn${selectedNodeName === n ? " active" : ""}`,
            "aria-pressed": selectedNodeName === n ? "true" : "false",
            textContent: n,
            onClick: () => selectSystemNode(selectedNodeName === n ? null : n),
          })
        ),
      ]);
      const scopeSelectorRow = el("div", { className: "filter-bar", style: "margin-bottom:0" }, [
        el("span", { className: "kpi-label", textContent: "Scope:" }),
        renderNodeScopeControl("Node scope"),
      ]);
      scopeRow = el("div", { style: "margin-bottom:10px" }, [targetNodesRow, scopeSelectorRow]);
    }

    const memMainVal =
      memUsedBytes !== null && memTotalBytes !== null
        ? formatBytePair(memUsedBytes, memTotalBytes)
        : memUsedBytes !== null
          ? bytesFmt(memUsedBytes)
          : memAvailBytes !== null
            ? `${bytesFmt(memAvailBytes)} avail`
            : "Unavailable";
    const diskRateText = formatByteRatePair(diskReadBps, diskWriteBps);
    const diskMainVal =
      diskRateText !== null
        ? diskRateText
        : diskAvailBytes !== null
          ? bytesFmt(diskAvailBytes)
          : "Unavailable";
    const diskFreeSub =
      diskAvailBytes !== null && diskTotalBytes !== null
        ? `Free: ${formatBytePair(diskAvailBytes, diskTotalBytes)}`
        : diskAvailBytes !== null
          ? `Free: ${bytesFmt(diskAvailBytes)}`
          : diskTotalBytes !== null
            ? `Free: N/A / ${bytesFmt(diskTotalBytes)}`
            : "Free: N/A";
    const netMainVal = formatByteRatePair(netRxBps, netTxBps) ?? "Unavailable";
    const netPeakSub = formatByteRatePair(netRxPeakBps, netTxPeakBps) ?? "N/A";

    const sysKpis = el("div", { className: "grid-kpis" }, [
      kpiTile("Host CPU Utilization", cpuAvg !== null ? pct(cpuAvg) : "Unavailable", `Peak: ${cpuPeak !== null ? pct(cpuPeak) : "N/A"}`),
      kpiTile(
        "Host Memory Usage",
        memMainVal,
        `Peak: ${memPeakPct !== null ? pct(memPeakPct) : "N/A"} | Min Avail: ${memAvailMinBytes !== null ? bytesFmt(memAvailMinBytes) : memAvailBytes !== null ? bytesFmt(memAvailBytes) : "N/A"}`
      ),
      kpiTile(
        "Disk I/O & Capacity",
        diskMainVal,
        diskFreeSub
      ),
      kpiTile(
        "Network Throughput",
        netMainVal,
        `Peak RX / TX: ${netPeakSub}`
      ),
      kpiTile(
        "Host Power",
        hostPowerAvg !== null ? `${num(hostPowerAvg, 1)} W` : "Unavailable",
        `Peak: ${hostPowerPeak !== null ? num(hostPowerPeak, 1) + " W" : "N/A"} | Energy: ${hostEnergyWh !== null ? num(hostEnergyWh, 2) + " Wh" : "N/A"}`
      ),
      kpiTile(
        `${accelLabel} Power & Thermal`,
        accPowerAvg !== null ? `${num(accPowerAvg, 1)} W` : "Unavailable",
        `Peak: ${accPowerPeak !== null ? num(accPowerPeak, 1) + " W" : "N/A"} | Peak Temp: ${accTempPeak !== null ? num(accTempPeak, 1) + " °C" : "N/A"}`,
        null,
        resolveHelpText("Accelerator Power & Thermal")
      ),
    ]);
    pageNode.appendChild(
      makeSection(
        "y-kpis",
        "Host System Summary",
        selectedNodeName ? `Scoped to node ${selectedNodeName}` : "Node-level CPU, memory, disk, network, and power telemetry",
        el("div", null, [scopeRow, sysKpis])
      )
    );

    if (perNode.length > 0) {
      const nodeTable = makeSortableTable(
        [
          {
            key: "node",
            label: "Node",
            render: (val) =>
              el("span", { className: "title-row" }, [
                el("span", { className: "mono", textContent: val || "unknown" }),
                selectedNodeName && val === selectedNodeName ? el("span", { className: "badge info", textContent: "selected" }) : null,
              ]),
          },
          {
            key: "roles",
            label: "Roles & Pods",
            getter: (r) => {
              const roles = Array.isArray(r.roles) ? r.roles.join(", ") : "";
              const cnt = firstFinite(r.pod_count) ?? (Array.isArray(r.pods) ? r.pods.length : 0);
              return `${roles}:${cnt}`;
            },
            render: (_raw, r) => {
              const roles = Array.isArray(r.roles) && r.roles.length ? r.roles : ["worker"];
              const cnt = firstFinite(r.pod_count) ?? (Array.isArray(r.pods) ? r.pods.length : 0);
              const tip = Array.isArray(r.pods) && r.pods.length ? r.pods.join(", ") : undefined;
              return el("span", { className: "title-row", title: tip }, [
                ...roles.map((role) => el("span", { className: "badge", textContent: role })),
                el("span", { className: "mono", textContent: `${intFmt(cnt)} pod${cnt === 1 ? "" : "s"}` }),
              ]);
            },
          },
          {
            key: "cpu",
            label: "CPU (Mean / Peak)",
            num: true,
            getter: (r) => clampPct(podStatMean(r, "cpu_usage_percent")),
            format: (_v, r) => {
              const m = clampPct(podStatMean(r, "cpu_usage_percent"));
              const pk = clampPct(statMax(r.cpu_usage_percent));
              return m !== null || pk !== null ? `${m !== null ? pct(m) : "N/A"} / ${pk !== null ? pct(pk) : "N/A"}` : "N/A";
            },
          },
          {
            key: "memory",
            label: "Memory (Used / Total · Peak %)",
            num: true,
            getter: (r) => firstFinite(clampPct(podStatMean(r, "memory_usage_percent")), podStatMean(r, "memory_used_bytes")),
            format: (_v, r) => {
              const used = podStatMean(r, "memory_used_bytes");
              const total = firstFinite(r.memory_total_bytes);
              const pk = clampPct(statMax(r.memory_usage_percent));
              if (used === null && total === null && pk === null) return "N/A";
              const memPair =
                used !== null && total !== null
                  ? formatBytePair(used, total)
                  : `${used !== null ? bytesFmt(used) : "N/A"} / ${total !== null ? bytesFmt(total) : "N/A"}`;
              return `${memPair} · ${pk !== null ? pct(pk) : "N/A"}`;
            },
          },
          {
            key: "disk_io",
            label: "Disk I/O (R / W)",
            num: true,
            getter: (r) => {
              const rd = podStatMean(r, "disk_read_bytes_per_second");
              const wr = podStatMean(r, "disk_written_bytes_per_second");
              return rd === null && wr === null ? null : (rd || 0) + (wr || 0);
            },
            format: (_v, r) => {
              const rd = podStatMean(r, "disk_read_bytes_per_second");
              const wr = podStatMean(r, "disk_written_bytes_per_second");
              return formatByteRatePair(rd, wr) ?? "N/A";
            },
          },
          {
            key: "free_disk",
            label: "Free Disk",
            num: true,
            getter: (r) => firstFinite(r.filesystem_avail_bytes),
            format: (avail, r) => {
              const total = firstFinite(r.filesystem_size_bytes);
              if (avail === null) return "N/A";
              return total !== null ? formatBytePair(avail, total) : bytesFmt(avail);
            },
          },
          {
            key: "network",
            label: "Network (RX / TX)",
            num: true,
            getter: (r) => {
              const rx = podStatMean(r, "network_receive_bytes_per_second");
              const tx = podStatMean(r, "network_transmit_bytes_per_second");
              return rx === null && tx === null ? null : (rx || 0) + (tx || 0);
            },
            format: (_v, r) => {
              const rx = podStatMean(r, "network_receive_bytes_per_second");
              const tx = podStatMean(r, "network_transmit_bytes_per_second");
              return formatByteRatePair(rx, tx) ?? "N/A";
            },
          },
          {
            key: "host_power",
            label: "Host Power (Mean / Peak)",
            num: true,
            getter: (r) => podStatMean(r, "host_power_watts"),
            format: (_v, r) => {
              const m = podStatMean(r, "host_power_watts");
              const pk = statMax(r.host_power_watts);
              return m !== null || pk !== null ? `${m !== null ? num(m, 1) : "N/A"} / ${pk !== null ? num(pk, 1) : "N/A"} W` : "N/A";
            },
          },
          {
            key: "accelerator",
            label: `${accelLabel} (Count · Util · Power · Temp)`,
            num: true,
            helpText: resolveHelpText("Accelerator (Count · Util · Power · Temp)"),
            getter: (r) => firstFinite(podStatMean(r, "gpu_utilization_percent"), podStatMean(r, "gpu_power_watts"), r.allocated_accelerators),
            format: (_v, r) => {
              const cnt = firstFinite(r.allocated_accelerators);
              const util = clampPct(podStatMean(r, "gpu_utilization_percent"));
              const pwr = podStatMean(r, "gpu_power_watts");
              const temp = firstFinite(statMax(r.gpu_temperature_celsius), podStatMean(r, "gpu_temperature_celsius"));
              if (cnt === null && util === null && pwr === null && temp === null) return "N/A";
              return `${cnt !== null ? intFmt(cnt) : "N/A"} · ${util !== null ? pct(util) : "N/A"} · ${pwr !== null ? num(pwr, 1) + " W" : "N/A"} · ${temp !== null ? num(temp, 1) + " °C" : "N/A"}`;
            },
          },
        ],
        perNode,
        {
          onRowClick: (row) => {
            if (row && row.node) selectSystemNode(selectedNodeName === row.node ? null : row.node);
          },
          isRowSelected: (row) => Boolean(selectedNodeName && row && row.node === selectedNodeName),
        }
      );
      pageNode.appendChild(
        makeSection("y-nodes", "Per-Node Summary", "Click any row to filter system charts to that node", nodeTable)
      );
    }

    if (nodeNames.length > 0) {
      pageNode.appendChild(
        el("div", { className: "section", "data-charts-scope": "true", style: "padding:10px 14px" }, [
          el("div", { className: "perf-controls", style: "margin-bottom:0" }, [
            el("span", { className: "kpi-label" }, [
              el("span", { textContent: "Charts Scope:" }),
              helpBadge("Charts Node Scope"),
            ]),
            renderNodeScopeControl("Charts node scope"),
          ]),
        ])
      );
    }

    const cpuHost = el("div", { className: "chart-box" });
    pageNode.appendChild(
      makeSection(
        "y-cpu",
        "Host CPU Utilization",
        selectedNodeName ? `Node ${selectedNodeName} CPU %` : "Target model pod host nodes",
        cpuHost
      )
    );
    const cpuSeriesList = [];
    if (selectedNodeName) {
      const pts = activeHostSeries.map((p) => ({
        x: p._t ?? p.t,
        y: clampPct(p.cpu_usage_percent ?? p.host_cpu_usage_percent ?? p.host_cpu_percent ?? p.cpu_pct),
      }));
      if (pts.some(isFiniteChartPoint)) {
        cpuSeriesList.push({ label: `${selectedNodeName} CPU %`, color: PALETTE[0], points: pts });
      }
    } else {
      const aggPts = hostSeries.map((p) => ({
        x: p._t ?? p.t,
        y: clampPct(p.host_cpu_usage_percent ?? p.host_cpu_percent ?? p.cpu_usage_percent ?? p.cpu_pct),
      }));
      const nodeEntries = Object.entries(nodeSeriesMap);
      const isMultiNode = nodeNames.length > 1 || nodeEntries.length > 1;
      if (hasSys && aggPts.some(isFiniteChartPoint)) {
        cpuSeriesList.push({
          label: isMultiNode ? "Host CPU % (Aggregate)" : "Host CPU %",
          color: PALETTE[0],
          points: aggPts,
        });
      }
      if (isMultiNode) {
        nodeEntries.forEach(([nodeName, nPts], idx) => {
          const mapped = nPts.map((p) => ({
            x: p._t ?? p.t,
            y: clampPct(p.cpu_usage_percent ?? p.host_cpu_usage_percent ?? p.host_cpu_percent ?? p.cpu_pct),
          }));
          if (mapped.some(isFiniteChartPoint)) {
            cpuSeriesList.push({
              label: `${nodeName} CPU %`,
              color: PALETTE[(idx + 1) % PALETTE.length],
              dashed: true,
              points: mapped,
            });
          }
        });
      }
    }
    const cpuMount = LineBandChart(cpuHost, {
      emptyText: note,
      yMin: 0,
      yMax: 100,
      series: cpuSeriesList,
    });

    const memUsedRawPts = hasSys
      ? activeHostSeries.map((p) => {
          const rawBytes = firstFinite(p.host_memory_used_bytes, p.memory_used_bytes);
          if (rawBytes !== null) return { x: p._t ?? p.t, y: Math.max(0, rawBytes) };
          const rawGb = firstFinite(p.host_mem_used_gb, p.host_memory_used_gb, p.mem_used_gb);
          return { x: p._t ?? p.t, y: rawGb !== null ? Math.max(0, rawGb) * BYTES_PER_GIB : null };
        })
      : [];
    const memAvailRawPts = hasSys
      ? activeHostSeries.map((p) => {
          const rawBytes = firstFinite(p.host_memory_available_bytes, p.memory_available_bytes);
          if (rawBytes !== null) return { x: p._t ?? p.t, y: Math.max(0, rawBytes) };
          const rawGb = firstFinite(p.host_mem_available_gb, p.host_memory_available_gb, p.mem_available_gb);
          return { x: p._t ?? p.t, y: rawGb !== null ? Math.max(0, rawGb) * BYTES_PER_GIB : null };
        })
      : [];
    const diskReadRawPts = hasSys
      ? activeHostSeries.map((p) => {
          const rawBps = firstFinite(p.host_disk_read_bytes_per_second, p.disk_read_bytes_per_second);
          if (rawBps !== null) return { x: p._t ?? p.t, y: Math.max(0, rawBps) };
          const rawMbps = firstFinite(p.host_disk_read_mbps, p.host_disk_read_mb_s, p.disk_read_mb_s);
          return { x: p._t ?? p.t, y: rawMbps !== null ? Math.max(0, rawMbps) * BYTES_PER_MIB : null };
        })
      : [];
    const diskWriteRawPts = hasSys
      ? activeHostSeries.map((p) => {
          const rawBps = firstFinite(
            p.host_disk_written_bytes_per_second,
            p.disk_written_bytes_per_second,
            p.host_disk_write_bytes_per_second,
            p.disk_write_bytes_per_second
          );
          if (rawBps !== null) return { x: p._t ?? p.t, y: Math.max(0, rawBps) };
          const rawMbps = firstFinite(p.host_disk_write_mbps, p.host_disk_write_mb_s, p.disk_write_mb_s);
          return { x: p._t ?? p.t, y: rawMbps !== null ? Math.max(0, rawMbps) * BYTES_PER_MIB : null };
        })
      : [];

    const memScale = selectByteScale(memUsedRawPts.concat(memAvailRawPts).map((pt) => pt.y));
    const diskScale = selectByteRateScale(diskReadRawPts.concat(diskWriteRawPts).map((pt) => pt.y));
    const scaleSeriesPts = (pts, divisor) => pts.map((pt) => ({ x: pt.x, y: pt.y !== null ? pt.y / divisor : null }));

    const memDiskSeries = [];
    let hasMemSeries = false;
    let hasDiskSeries = false;
    if (hasSys) {
      const memUsedPts = scaleSeriesPts(memUsedRawPts, memScale.divisor);
      const memAvailPts = scaleSeriesPts(memAvailRawPts, memScale.divisor);
      const diskReadPts = scaleSeriesPts(diskReadRawPts, diskScale.divisor);
      const diskWritePts = scaleSeriesPts(diskWriteRawPts, diskScale.divisor);
      const fmtMemTip = (v) => `${fmtDec(v)} ${memScale.unit}`;
      const fmtDiskTip = (v) => `${fmtDec(v)} ${diskScale.unit}`;
      if (memUsedPts.some(isFiniteChartPoint)) {
        memDiskSeries.push({ label: `Mem Used (${memScale.unit})`, color: PALETTE[0], points: memUsedPts, formatValue: fmtMemTip });
        hasMemSeries = true;
      }
      if (memAvailPts.some(isFiniteChartPoint)) {
        memDiskSeries.push({ label: `Mem Available (${memScale.unit})`, color: PALETTE[5], dashed: true, points: memAvailPts, formatValue: fmtMemTip });
        hasMemSeries = true;
      }
      if (diskReadPts.some(isFiniteChartPoint)) {
        memDiskSeries.push({ label: `Disk Read (${diskScale.unit})`, axis: "right", color: PALETTE[1], points: diskReadPts, formatValue: fmtDiskTip });
        hasDiskSeries = true;
      }
      if (diskWritePts.some(isFiniteChartPoint)) {
        memDiskSeries.push({ label: `Disk Write (${diskScale.unit})`, axis: "right", color: PALETTE[2], points: diskWritePts, formatValue: fmtDiskTip });
        hasDiskSeries = true;
      }
    }
    const memUnitSuffix = hasMemSeries ? ` (${memScale.unit})` : "";
    const diskUnitSuffix = hasDiskSeries ? ` (${diskScale.unit})` : "";
    const memDiskCoreSub = `Memory used / available${memUnitSuffix} & Disk read / write throughput${diskUnitSuffix}`;

    const memHost = el("div", { className: "chart-box" });
    pageNode.appendChild(
      makeSection(
        "y-mem-disk",
        "Host Memory & Disk I/O",
        selectedNodeName ? `Node ${selectedNodeName}: ${memDiskCoreSub}` : memDiskCoreSub,
        memHost
      )
    );
    const memMount = LineBandChart(memHost, {
      emptyText: note,
      rightLabel: hasDiskSeries ? diskScale.unit : undefined,
      series: memDiskSeries,
    });

    const activeNetPoints = resolveActiveNetPoints(selectedNodeName, activeHostSeries, hostSeries, nodeSeriesMap);
    const availableNics = collectAvailableNics(selectedNodeName, selectedNodeObj, sys, perNode, activeNetPoints);
    const activeNic =
      state.systemNic === BY_NIC_SCOPE && availableNics.length >= 2
        ? BY_NIC_SCOPE
        : state.systemNic && availableNics.includes(state.systemNic)
          ? state.systemNic
          : null;

    const rawNetSeries = [];
    if (hasSys || availableNics.length > 0) {
      if (activeNic === BY_NIC_SCOPE) {
        const plottedNicGroups =
          availableNics.length <= 5
            ? availableNics.map((dev, idx) => ({ label: dev, devices: [dev], color: PALETTE[idx] }))
            : [
                ...availableNics.slice(0, 4).map((dev, idx) => ({ label: dev, devices: [dev], color: PALETTE[idx] })),
                { label: "Other", devices: availableNics.slice(4), color: PALETTE[4] },
              ];
        for (const grp of plottedNicGroups) {
          const { rxPts, txPts } = buildNicDirectionPoints(activeNetPoints, grp.devices);
          if (rxPts.some(isFiniteChartPoint)) {
            rawNetSeries.push({ label: `${grp.label} RX`, color: grp.color, points: rxPts });
          }
          if (txPts.some(isFiniteChartPoint)) {
            rawNetSeries.push({ label: `${grp.label} TX`, color: grp.color, dashed: true, points: txPts });
          }
        }
      } else if (activeNic) {
        const { rxPts, txPts } = buildNicDirectionPoints(activeNetPoints, [activeNic]);
        if (rxPts.some(isFiniteChartPoint)) {
          rawNetSeries.push({ label: `${activeNic} RX`, color: PALETTE[0], points: rxPts });
        }
        if (txPts.some(isFiniteChartPoint)) {
          rawNetSeries.push({ label: `${activeNic} TX`, color: PALETTE[1], points: txPts });
        }
      } else {
        const rxPts = activeNetPoints.map((p) => {
          const x = p._t ?? p.t;
          const rawBps = firstFinite(p.host_network_receive_bytes_per_second, p.network_receive_bytes_per_second);
          if (rawBps !== null) return { x, y: clampNonNegMetric(rawBps) };
          const directMbps = firstFinite(p.host_network_receive_mbps, p.network_receive_mbps);
          if (directMbps !== null) return { x, y: clampNonNegMetric(directMbps) * BYTES_PER_MIB };
          if (hasPointNicMap(p)) {
            const { rxPts: summed } = buildNicDirectionPoints([p], Object.keys(p.network_interfaces));
            return summed[0];
          }
          return { x, y: null };
        });
        const txPts = activeNetPoints.map((p) => {
          const x = p._t ?? p.t;
          const rawBps = firstFinite(p.host_network_transmit_bytes_per_second, p.network_transmit_bytes_per_second);
          if (rawBps !== null) return { x, y: clampNonNegMetric(rawBps) };
          const directMbps = firstFinite(p.host_network_transmit_mbps, p.network_transmit_mbps);
          if (directMbps !== null) return { x, y: clampNonNegMetric(directMbps) * BYTES_PER_MIB };
          if (hasPointNicMap(p)) {
            const { txPts: summed } = buildNicDirectionPoints([p], Object.keys(p.network_interfaces));
            return summed[0];
          }
          return { x, y: null };
        });
        if (rxPts.some(isFiniteChartPoint)) rawNetSeries.push({ label: "Network RX", color: PALETTE[0], points: rxPts });
        if (txPts.some(isFiniteChartPoint)) rawNetSeries.push({ label: "Network TX", color: PALETTE[1], points: txPts });
      }
    }

    const netScale = selectByteRateScale(rawNetSeries.flatMap((s) => s.points.map((pt) => pt.y)));
    const netSeries = rawNetSeries.map((s) => ({
      ...s,
      label: `${s.label} (${netScale.unit})`,
      points: scaleSeriesPts(s.points, netScale.divisor),
    }));

    let nicFilterBar = null;
    if (availableNics.length > 0) {
      const nicOptions = [
        { value: "__all__", label: "All NICs (Total)" },
        ...(availableNics.length >= 2 ? [{ value: BY_NIC_SCOPE, label: "By NIC" }] : []),
        ...availableNics.map((dev) => ({ value: dev, label: dev })),
      ];
      const currentNicVal = activeNic || "__all__";
      const nicControl =
        availableNics.length <= 3
          ? el(
              "div",
              { className: "filter-bar", style: "margin-bottom:0" },
              nicOptions.map((opt) =>
                el("button", {
                  type: "button",
                  className: `pill-btn${currentNicVal === opt.value ? " active" : ""}`,
                  textContent: opt.label,
                  onClick: () => selectSystemNic(opt.value === "__all__" ? null : opt.value),
                })
              )
            )
          : makeSelect(
              {
                className: "input",
                "aria-label": "Network interface",
                onChange: (e) => selectSystemNic(e.target.value === "__all__" ? null : e.target.value),
              },
              nicOptions,
              currentNicVal
            );
      nicFilterBar = el("div", { className: "filter-bar" }, [
        el("span", { className: "kpi-label" }, [
          el("span", { textContent: "Interface:" }),
          helpBadge("Network Interface"),
        ]),
        nicControl,
      ]);
    }

    const netHost = el("div", { className: "chart-box" });
    const netLegacyHint =
      availableNics.length === 0 && netSeries.length > 0
        ? el("div", {
            className: "section-sub",
            style: "margin-top:6px",
            textContent: "Per-interface breakdown is unavailable for this telemetry snapshot (showing aggregate RX / TX).",
          })
        : null;
    const netUnitSuffix = netSeries.length > 0 ? ` (${netScale.unit})` : "";
    const netSubtitle =
      activeNic === BY_NIC_SCOPE
        ? selectedNodeName
          ? `Node ${selectedNodeName} per-NIC network RX / TX${netUnitSuffix}`
          : `Aggregate per-NIC network RX / TX throughput${netUnitSuffix}`
        : activeNic
          ? selectedNodeName
            ? `Node ${selectedNodeName} · ${activeNic} RX / TX${netUnitSuffix}`
            : `All Nodes · ${activeNic} RX / TX${netUnitSuffix}`
          : selectedNodeName
            ? `Node ${selectedNodeName} network RX / TX${netUnitSuffix}`
            : `Host network receive and transmit throughput${netUnitSuffix}`;
    pageNode.appendChild(
      makeSection(
        "y-net",
        "Network RX / TX Throughput",
        netSubtitle,
        el("div", null, [nicFilterBar, netHost, netLegacyHint].filter(Boolean))
      )
    );
    const netMount = LineBandChart(netHost, {
      emptyText: "Network RX/TX telemetry unavailable (node_exporter network metrics not detected).",
      yUnit: netScale.unit,
      series: netSeries,
    });

    const pwrHost = el("div", { className: "chart-box" });
    pageNode.appendChild(
      makeSection(
        "y-power",
        `Host & ${accelLabel} Power`,
        selectedNodeName
          ? `Node ${selectedNodeName} power vs ${accelLabel.toLowerCase()} power (W)`
          : `Node power vs serving ${accelLabel.toLowerCase()} power (W)`,
        pwrHost,
        null,
        resolveHelpText("Host & Accelerator Power")
      )
    );
    const hostPwrPts = activeHostSeries.map((p) => ({
      x: p._t ?? p.t,
      y: firstFinite(p.host_power_watts, p.host_power_w),
    }));
    const accelPwrSrc = activeAccelSeries.some((p) => isFiniteMetric(p.gpu_power_watts ?? p.accelerator_power_watts))
      ? activeAccelSeries
      : (!selectedNodeName ? hostSeries : []);
    const accelPwrPts = accelPwrSrc.map((p) => ({
      x: p._t ?? p.t,
      y: firstFinite(p.gpu_power_watts, p.accelerator_power_watts),
    }));
    const pwrMount = LineBandChart(pwrHost, {
      emptyText: note,
      series: [
        ...(hostPwrPts.some(isFiniteChartPoint) ? [{ label: "Host Power (W)", color: PALETTE[1], points: hostPwrPts }] : []),
        ...(accelPwrPts.some(isFiniteChartPoint) ? [{ label: `${accelLabel} Power (W)`, color: PALETTE[2], points: accelPwrPts }] : []),
      ],
    });
    pageNode._systemMounts = [cpuMount, memMount, netMount, pwrMount].filter(Boolean);
    pageNode._systemSubs = state.crosshairSubscribers.slice(subStart);
    scheduleReflow(pageNode);
  }

  function jumpToSystemNode(nodeName) {
    state.systemNode = nodeName || null;
    state.systemNic = null;
    renderAllPages();
    switchPage("system");
  }

  function renderHardware(pageNode) {
    const d = state.data || {};
    const t = d.task || {};
    const st = d.server_telemetry || {};
    const sys = st.system || st.host_metrics || {};
    const summary = st.summary || {};
    const manifest = d.run_manifest || {};
    const manifestTarget = manifest.target || {};
    const hwProfiles = Array.isArray(st.hardware_profiles) ? st.hardware_profiles : [];
    const primaryProfile = hwProfiles[0] || st.hardware_profile || manifest.hardware_profile || null;
    const accelLabel = getHwLabel(st);
    const rawPerNode = Array.isArray(st.per_node) ? st.per_node : [];
    const rawPerPod = (Array.isArray(st.per_pod) ? st.per_pod : []).filter(
      (p) => p && effectiveReportPodRole(p) !== "runner"
    );
    const cacheConfig = st.cache_config || st.cache_config_info || {};
    const cacheEngines = Array.isArray(cacheConfig.engines) ? cacheConfig.engines : [];
    const rawTopo = resolveRawTopologyMode(st, d);
    const isDisaggregated = isDisaggregatedReport(st, d);

    const clusterName = t.cluster_name || manifestTarget.cluster_name || null;
    const namespace = t.namespace || manifestTarget.namespace || null;
    const deploymentName = t.deployment_name || manifestTarget.deployment_name || null;
    const endpointUrl = t.sanitized_endpoint_url || manifestTarget.sanitized_endpoint_url || null;
    const endpointMode = t.endpoint_mode || manifestTarget.endpoint_mode || (clusterName || namespace ? "deployment" : "external");
    const topoLabel =
      rawTopo === "disaggregated"
        ? "P/D Disaggregation"
        : rawTopo === "aggregated"
          ? "Aggregated"
          : st.topology_mode || manifestTarget.topology_mode || null;
    const targetVal =
      endpointMode === "deployment"
        ? [clusterName, namespace].filter(Boolean).join(" / ") || deploymentName || "Deployment"
        : "External Endpoint";
    const targetSub =
      [
        topoLabel,
        st.guide_type ? `Guide: ${st.guide_type}` : null,
        endpointMode === "deployment" && deploymentName && (clusterName || namespace) ? deploymentName : null,
        endpointMode === "external" && endpointUrl ? endpointUrl : null,
      ]
        .filter(Boolean)
        .join(" · ") || "Target metadata";

    const profileModels = [];
    const seenModels = new Set();
    const pushModel = (m) => {
      const s = typeof m === "string" ? m.trim() : "";
      if (s && !seenModels.has(s)) {
        seenModels.add(s);
        profileModels.push(s);
      }
    };
    hwProfiles.forEach((prof) => {
      if (prof && Array.isArray(prof.models)) prof.models.forEach(pushModel);
    });
    if (!profileModels.length && primaryProfile && Array.isArray(primaryProfile.models)) {
      primaryProfile.models.forEach(pushModel);
    }
    const accelSummary = typeof st.accelerator_summary === "string" && st.accelerator_summary.trim() ? st.accelerator_summary.trim() : null;
    const recordedHw = typeof t.hardware === "string" && t.hardware.trim() ? t.hardware.trim() : null;
    const profileVal =
      accelSummary ||
      (profileModels.length ? profileModels.join(", ") : null) ||
      (primaryProfile && (primaryProfile.display_name || primaryProfile.id)) ||
      recordedHw ||
      "Not reported";
    const profileSub =
      [
        primaryProfile && primaryProfile.vendor ? `Vendor: ${primaryProfile.vendor}` : null,
        primaryProfile && primaryProfile.display_name && profileVal !== primaryProfile.display_name
          ? primaryProfile.display_name
          : null,
        recordedHw && profileVal !== recordedHw ? `Recorded: ${recordedHw}` : null,
      ]
        .filter(Boolean)
        .join(" · ") ||
      (primaryProfile && primaryProfile.metric_group ? `Group: ${primaryProfile.metric_group}` : "Hardware profile metadata");

    const nodeCount =
      rawPerNode.length ||
      (Array.isArray(st.nodes) ? st.nodes.length : Array.isArray(sys.nodes) ? sys.nodes.length : 0);
    const podCount =
      rawPerPod.length ||
      (Array.isArray(st.pods) ? st.pods.length : Array.isArray(manifestTarget.pods) ? manifestTarget.pods.length : 0);
    const roleCounts = Object.create(null);
    rawPerPod.forEach((p) => {
      const role = effectiveReportPodRole(p) || p.role || "worker";
      roleCounts[role] = (roleCounts[role] || 0) + 1;
    });
    const roleBreakdownParts = [];
    if (roleCounts.prefill) roleBreakdownParts.push(`Prefill: ${intFmt(roleCounts.prefill)}`);
    if (roleCounts.decode) roleBreakdownParts.push(`Decode: ${intFmt(roleCounts.decode)}`);
    if (roleCounts.worker) roleBreakdownParts.push(`Worker: ${intFmt(roleCounts.worker)}`);
    if (roleCounts.epp) roleBreakdownParts.push(`Router: ${intFmt(roleCounts.epp)}`);
    Object.keys(roleCounts).forEach((r) => {
      if (!["prefill", "decode", "worker", "epp"].includes(r) && roleCounts[r]) {
        roleBreakdownParts.push(`${r}: ${intFmt(roleCounts[r])}`);
      }
    });
    const nodesPodsVal =
      nodeCount > 0 || podCount > 0
        ? `${intFmt(nodeCount)} node${nodeCount === 1 ? "" : "s"} · ${intFmt(podCount)} pod${podCount === 1 ? "" : "s"}`
        : "Unavailable";
    const nodesPodsSub = roleBreakdownParts.length
      ? roleBreakdownParts.join(" · ")
      : podCount > 0
        ? "Serving pod inventory"
        : "Node & pod inventory unavailable";

    const sumFiniteField = (items, getter) => {
      let sum = 0;
      let any = false;
      for (const item of items) {
        const v = getter(item);
        if (v !== null && v >= 0) {
          sum += v;
          any = true;
        }
      }
      return any ? sum : null;
    };
    const totalAllocFromNodes = sumFiniteField(rawPerNode, (n) => firstFinite(n.allocated_accelerators));
    const totalAllocFromProfiles = sumFiniteField(hwProfiles, (p) => firstFinite(p.allocated_accelerators));
    const totalAllocFromPrimary = primaryProfile
      ? firstFinite(primaryProfile.allocated_accelerators, primaryProfile.device_count)
      : null;
    const totalAllocFromPods = sumFiniteField(
      rawPerPod.filter((p) => effectiveReportPodRole(p) !== "epp"),
      (p) => firstFinite(p.allocated_accelerators)
    );
    const totalAlloc = firstFinite(
      totalAllocFromNodes,
      totalAllocFromProfiles,
      totalAllocFromPrimary,
      totalAllocFromPods
    );
    const totalAccelCap = sumFiniteField(rawPerNode, (n) => firstFinite(n.accelerator_capacity));
    const totalDevMemFromNodes = sumFiniteField(rawPerNode, (n) =>
      firstFinite(n.gpu_memory_total_bytes, podStatMean(n, "gpu_memory_total_bytes"))
    );
    const totalDevMemFromPods = sumFiniteField(rawPerPod, (p) =>
      firstFinite(podStatMean(p, "gpu_memory_total_bytes"), p.gpu_memory_total_bytes)
    );
    const totalDevMemBytes = firstFinite(
      totalDevMemFromNodes,
      podStatMean(summary, "gpu_memory_total_bytes"),
      totalDevMemFromPods
    );
    const accelVal =
      totalAlloc !== null && totalDevMemBytes !== null
        ? `${intFmt(totalAlloc)} · ${bytesFmt(totalDevMemBytes)}`
        : totalAlloc !== null
          ? `${intFmt(totalAlloc)} allocated`
          : totalDevMemBytes !== null
            ? bytesFmt(totalDevMemBytes)
            : "Unavailable";
    const accelSub =
      [
        totalAlloc !== null && totalAlloc > 0 && totalDevMemBytes !== null
          ? `${bytesFmt(totalDevMemBytes / totalAlloc)} / device`
          : null,
        totalAccelCap !== null ? `Node Capacity: ${intFmt(totalAccelCap)}` : null,
      ]
        .filter(Boolean)
        .join(" · ") ||
      (totalAlloc === null && totalDevMemBytes === null
        ? "Device allocation telemetry unavailable"
        : "Allocated serving accelerators");

    const totalCpuCap = sumFiniteField(rawPerNode, (n) => firstFinite(n.cpu_capacity_cores));
    const totalCpuAlloc = sumFiniteField(rawPerNode, (n) => firstFinite(n.cpu_allocatable_cores));
    const totalHostMemBytes = firstFinite(
      sumFiniteField(rawPerNode, (n) => firstFinite(n.memory_capacity_bytes, n.memory_total_bytes)),
      sys.mem_total_bytes
    );
    const hostValParts = [];
    if (totalCpuCap !== null) hostValParts.push(`${fmtDec(totalCpuCap)} vCPU`);
    if (totalHostMemBytes !== null) hostValParts.push(`${bytesFmt(totalHostMemBytes)} RAM`);
    const hostVal = hostValParts.length ? hostValParts.join(" · ") : "Unavailable";
    const hostSub =
      [
        totalCpuAlloc !== null ? `Allocatable: ${fmtDec(totalCpuAlloc)} vCPU` : null,
        rawPerNode.length ? `${intFmt(rawPerNode.length)} serving node${rawPerNode.length === 1 ? "" : "s"}` : null,
      ]
        .filter(Boolean)
        .join(" · ") || "Host compute specs unavailable";

    const overviewKpis = el("div", { className: "grid-kpis" }, [
      kpiTile("Target Environment", targetVal, targetSub),
      kpiTile("Accelerator Profile", profileVal, profileSub),
      kpiTile("Serving Nodes & Pods", nodesPodsVal, nodesPodsSub),
      kpiTile("Allocated Accelerators & Device Memory", accelVal, accelSub),
      kpiTile("Host Compute & Memory", hostVal, hostSub),
    ]);
    pageNode.appendChild(
      makeSection(
        "h-overview",
        "Hardware Overview",
        "Target environment, accelerator profile, and cluster hardware capacity",
        overviewKpis
      )
    );

    // #h-nodes · Node Hardware Specifications
    const fallbackNoteText =
      endpointMode === "external" || st.status === "unavailable"
        ? "Node-level hardware inventory is unavailable for external endpoints or runs without cluster telemetry."
        : "No participating node hardware specifications were recorded for this run.";
    let nodesBody;
    if (rawPerNode.length > 0) {
      nodesBody = makeSortableTable(
        [
          {
            key: "node",
            label: "Node",
            helpText: resolveHelpText("Node"),
            helpFormula: "Node = metadata.name (click row to inspect host & device telemetry)",
            render: (val) => el("span", { className: "mono", textContent: val || "unknown" }),
          },
          {
            key: "roles",
            label: "Roles & Pods",
            helpText: resolveHelpText("Roles & Pods"),
            helpFormula: resolveHelpFormula("Roles & Pods"),
            getter: (r) => {
              const roles = Array.isArray(r.roles) ? r.roles.join(", ") : "";
              const cnt = firstFinite(r.pod_count) ?? (Array.isArray(r.pods) ? r.pods.length : 0);
              return `${roles}:${cnt}`;
            },
            render: (_raw, r) => {
              const roles = Array.isArray(r.roles) && r.roles.length ? r.roles : ["worker"];
              const cnt = firstFinite(r.pod_count) ?? (Array.isArray(r.pods) ? r.pods.length : 0);
              const tip = Array.isArray(r.pods) && r.pods.length ? r.pods.join(", ") : undefined;
              return el("span", { className: "title-row", title: tip }, [
                ...roles.map((role) => el("span", { className: "badge", textContent: role })),
                el("span", { className: "mono", textContent: `${intFmt(cnt)} pod${cnt === 1 ? "" : "s"}` }),
              ]);
            },
          },
          {
            key: "accelerator",
            label: "Accelerator",
            helpText: "Accelerator model(s) and allocated-to-capacity device count on this node.",
            helpFormula: "Devices = Allocated_Accelerators / Node_Device_Capacity (fallback: device slice count)",
            getter: (r) => firstFinite(r.allocated_accelerators, r.accelerator_capacity) ?? -1,
            render: (_raw, r) => {
              const alloc = firstFinite(r.allocated_accelerators);
              const cap = firstFinite(r.accelerator_capacity);
              const model =
                (typeof r.accelerator_model === "string" && r.accelerator_model.trim()) ||
                (profileModels.length ? profileModels.join(", ") : null) ||
                (primaryProfile && primaryProfile.display_name) ||
                null;
              const countStr =
                alloc !== null && cap !== null
                  ? `${intFmt(alloc)} / ${intFmt(cap)}`
                  : alloc !== null
                    ? intFmt(alloc)
                    : cap !== null
                      ? `0 / ${intFmt(cap)}`
                      : null;
              const text = [countStr, model].filter(Boolean).join(" · ") || "N/A";
              return el("span", { className: "mono", textContent: text });
            },
          },
          {
            key: "device_memory",
            label: "Device Memory",
            num: true,
            helpText: "Total accelerator device memory (HBM) aggregated across serving pods on this node.",
            helpFormula: "Device_Memory = Σ_node_pods Device_Memory_Total_Bytes",
            getter: (r) => firstFinite(r.gpu_memory_total_bytes, podStatMean(r, "gpu_memory_total_bytes")),
            format: (v) => (v !== null ? bytesFmt(v) : "N/A"),
          },
          {
            key: "cpu_cores",
            label: "CPU (Alloc / Cap)",
            num: true,
            helpText: "Allocatable versus physical capacity CPU cores reported by the node.",
            helpFormula: "CPU_Cores = status.allocatable.cpu / status.capacity.cpu",
            getter: (r) => firstFinite(r.cpu_capacity_cores, r.cpu_allocatable_cores),
            format: (_v, r) => {
              const alloc = firstFinite(r.cpu_allocatable_cores);
              const cap = firstFinite(r.cpu_capacity_cores);
              if (alloc === null && cap === null) return "N/A";
              if (alloc !== null && cap !== null) return `${fmtDec(alloc)} / ${fmtDec(cap)} vCPU`;
              return `${fmtDec(cap ?? alloc)} vCPU`;
            },
          },
          {
            key: "host_memory",
            label: "Host Memory",
            num: true,
            helpText: "Physical host RAM capacity from node status (falling back to host telemetry total memory).",
            helpFormula: "Host_Memory = status.capacity.memory ?? Host_Memory_Total_Bytes",
            getter: (r) => firstFinite(r.memory_capacity_bytes, r.memory_total_bytes),
            format: (v) => (v !== null ? bytesFmt(v) : "N/A"),
          },
          {
            key: "platform",
            label: "Arch & OS",
            helpText: "Node CPU architecture, operating system image, kernel, kubelet, and container runtime versions.",
            helpFormula: "Platform = nodeInfo.{architecture, osImage, kernelVersion, kubeletVersion, containerRuntimeVersion}",
            getter: (r) =>
              [r.architecture, r.os_image, r.kernel_version, r.kubelet_version, r.container_runtime]
                .filter(Boolean)
                .join(" · "),
            render: (_raw, r) => {
              const primary = [r.architecture, r.os_image].filter(Boolean).join(" · ");
              const detail = [r.kernel_version, r.kubelet_version, r.container_runtime].filter(Boolean).join(" · ");
              if (!primary && !detail) return el("span", { textContent: "N/A" });
              return el("div", { title: detail || primary }, [
                el("div", { className: "mono", textContent: primary || detail }),
                primary && detail ? el("div", { className: "kpi-sub mono", textContent: detail }) : null,
              ]);
            },
          },
        ],
        rawPerNode,
        {
          onRowClick: (row) => {
            if (row && row.node) jumpToSystemNode(row.node);
          },
        }
      );
    } else {
      nodesBody = el("div", { className: "empty-note", textContent: fallbackNoteText });
    }
    pageNode.appendChild(
      makeSection(
        "h-nodes",
        "Node Hardware Specifications",
        rawPerNode.length
          ? `${intFmt(rawPerNode.length)} serving node${rawPerNode.length === 1 ? "" : "s"} · click any row to open ${accelLabel} & host telemetry`
          : "Participating node hardware inventory",
        nodesBody
      )
    );

    // #h-pods · Serving Pod Hardware Allocation
    let podsBody;
    if (rawPerPod.length > 0) {
      podsBody = makeSortableTable(
        [
          {
            key: "pod",
            label: "Pod",
            helpText: resolveHelpText("Pod"),
            helpFormula: "Pod = metadata.name (target serving or routing pod)",
            render: (val) => el("span", { className: "mono", textContent: val || "unknown" }),
          },
          {
            key: "role",
            label: "Role",
            helpText: resolveHelpText("Role"),
            helpFormula: "Role ∈ {worker, prefill, decode, epp}",
            getter: (r) => effectiveReportPodRole(r) || r.role || "worker",
            render: (val) => el("span", { className: "badge", textContent: val }),
          },
          {
            key: "node",
            label: "Node",
            helpText: resolveHelpText("Node"),
            helpFormula: "Node = spec.nodeName (click to open System view for this node)",
            render: (val) =>
              val
                ? el("button", {
                    type: "button",
                    className: "evidence-btn mono",
                    textContent: val,
                    onClick: (e) => {
                      e.stopPropagation();
                      jumpToSystemNode(val);
                    },
                  })
                : el("span", { textContent: "N/A" }),
          },
          {
            key: "allocated_accelerators",
            label: "Allocated Accelerators",
            num: true,
            helpText: "Accelerator devices allocated to this pod from device allocations or container requests/limits.",
            helpFormula: "Pod_Devices = Allocated_Device_Count ?? max(limits[res], requests[res])",
            getter: (r) => firstFinite(r.allocated_accelerators),
            format: (v) => (v !== null ? intFmt(v) : "N/A"),
          },
          {
            key: "device_memory",
            label: "Device Memory",
            num: true,
            helpText: "Total accelerator device memory (HBM) visible to this serving pod.",
            helpFormula: "Pod_Device_Memory = mean_t(Device_Memory_Total_Bytes_pod(t))",
            getter: (r) => firstFinite(podStatMean(r, "gpu_memory_total_bytes"), r.gpu_memory_total_bytes),
            format: (v) => (v !== null ? bytesFmt(v) : "N/A"),
          },
          {
            key: "cpu_req_limit",
            label: "CPU (Req / Limit)",
            num: true,
            helpText: "Sum of CPU requests and limits across containers in this serving pod.",
            helpFormula: "CPU_Req_Limit = Σ_containers requests.cpu / Σ_containers limits.cpu",
            getter: (r) => firstFinite(r.cpu_limit_cores, r.cpu_request_cores),
            format: (_v, r) => {
              const req = firstFinite(r.cpu_request_cores);
              const lim = firstFinite(r.cpu_limit_cores);
              if (req === null && lim === null) return "N/A";
              return `${req !== null ? fmtDec(req) : "N/A"} / ${lim !== null ? fmtDec(lim) : "N/A"} vCPU`;
            },
          },
          {
            key: "memory_req_limit",
            label: "Memory (Req / Limit)",
            num: true,
            helpText: "Sum of host memory requests and limits across containers in this serving pod.",
            helpFormula: "Mem_Req_Limit = Σ_containers requests.memory / Σ_containers limits.memory",
            getter: (r) => firstFinite(r.memory_limit_bytes, r.memory_request_bytes),
            format: (_v, r) => {
              const req = firstFinite(r.memory_request_bytes);
              const lim = firstFinite(r.memory_limit_bytes);
              if (req === null && lim === null) return "N/A";
              return formatBytePair(req, lim);
            },
          },
        ],
        rawPerPod
      );
    } else {
      podsBody = el("div", {
        className: "empty-note",
        textContent: "No per-pod hardware allocation records were captured for this run.",
      });
    }
    pageNode.appendChild(
      makeSection(
        "h-pods",
        "Serving Pod Hardware Allocation",
        "Per-pod accelerator, CPU, and memory resource allocations",
        podsBody
      )
    );

    // #h-cache · KV Cache & Memory Hierarchy
    const eng0 = cacheEngines[0] || {};
    const hbmTokens = positiveFinite(cacheConfig.hbm_capacity_tokens ?? eng0.kv_cache_size_tokens);
    const hbmBytes = positiveFinite(cacheConfig.hbm_capacity_bytes);
    const cpuTokens = positiveFinite(cacheConfig.cpu_capacity_tokens);
    const cpuBytes = positiveFinite(cacheConfig.cpu_capacity_bytes);
    const diskTokens = positiveFinite(cacheConfig.nvme_capacity_tokens ?? cacheConfig.disk_capacity_tokens);
    const diskBytes = positiveFinite(cacheConfig.nvme_capacity_bytes ?? cacheConfig.disk_capacity_bytes);
    const effTokens = positiveFinite(cacheConfig.effective_capacity_tokens ?? hbmTokens);
    const blkSize = positiveFinite(cacheConfig.block_size ?? eng0.block_size);
    const devBlocks = positiveFinite(cacheConfig.num_gpu_blocks ?? eng0.num_gpu_blocks);
    const maxConc = positiveFinite(cacheConfig.kv_cache_max_concurrency ?? eng0.kv_cache_max_concurrency);
    const prefillHbmCap = positiveFinite(cacheConfig.prefill_hbm_capacity_tokens);
    const decodeHbmCap = positiveFinite(cacheConfig.decode_hbm_capacity_tokens);

    const hasCacheCaps = [hbmTokens, hbmBytes, cpuTokens, cpuBytes, diskTokens, diskBytes, effTokens].some(
      (v) => v !== null
    );
    let cacheBody;
    if (hasCacheCaps) {
      const hbmVal = hbmTokens !== null ? `${intFmt(hbmTokens)} tok` : hbmBytes !== null ? bytesFmt(hbmBytes) : "Not configured";
      const hbmSub =
        [
          hbmTokens !== null && hbmBytes !== null ? bytesFmt(hbmBytes) : null,
          isDisaggregated && prefillHbmCap !== null && decodeHbmCap !== null
            ? `Prefill: ${intFmt(prefillHbmCap)} · Decode: ${intFmt(decodeHbmCap)} tok`
            : null,
          blkSize !== null ? `Block: ${intFmt(blkSize)} tok` : null,
          devBlocks !== null ? `Blocks: ${intFmt(devBlocks)}` : null,
        ]
          .filter(Boolean)
          .join(" · ") || "Device memory KV pool";

      const cpuVal =
        cpuTokens !== null
          ? `${intFmt(cpuTokens)} tok`
          : cpuBytes !== null
            ? bytesFmt(cpuBytes)
            : cacheConfig.host_kv_configured
              ? "Configured"
              : "Not configured";
      const cpuSub =
        cpuTokens !== null && cpuBytes !== null
          ? bytesFmt(cpuBytes)
          : cacheConfig.host_kv_configured
            ? "Host CPU KV offload active"
            : "Host CPU KV offload unconfigured";

      const diskVal =
        diskTokens !== null
          ? `${intFmt(diskTokens)} tok`
          : diskBytes !== null
            ? bytesFmt(diskBytes)
            : cacheConfig.nvme_kv_configured
              ? "Configured"
              : "Not configured";
      const diskSub =
        diskTokens !== null && diskBytes !== null
          ? bytesFmt(diskBytes)
          : cacheConfig.nvme_kv_configured
            ? "Local storage KV offload active"
            : "Local storage KV tier unconfigured";

      const effVal = effTokens !== null ? `${intFmt(effTokens)} tok` : "N/A";
      const effSub =
        [
          maxConc !== null ? `Max KV Concurrency: ${fmtDec(maxConc)}×` : null,
          cacheEngines.length ? `${intFmt(cacheEngines.length)} engine${cacheEngines.length === 1 ? "" : "s"}` : null,
        ]
          .filter(Boolean)
          .join(" · ") || "Combined usable KV capacity";

      cacheBody = el("div", { className: "grid-kpis" }, [
        kpiTile("Device HBM KV Capacity", hbmVal, hbmSub),
        kpiTile("Host CPU KV Offload Capacity", cpuVal, cpuSub),
        kpiTile("Local Storage KV Capacity", diskVal, diskSub),
        kpiTile("Effective KV Cache Capacity", effVal, effSub),
      ]);
    } else {
      cacheBody = el("div", {
        className: "empty-note",
        textContent: "KV cache capacity hierarchy metadata was not exposed for this run.",
      });
    }
    pageNode.appendChild(
      makeSection(
        "h-cache",
        "KV Cache & Memory Hierarchy",
        "Configured device HBM, host CPU offload, and local storage KV cache capacities",
        cacheBody
      )
    );
  }

  const LOG_ANSI_RE = new RegExp(`${String.fromCharCode(27)}\\[[0-9;]*[A-Za-z]`, "g");

  const EXPLICIT_LOG_LEVEL_PATTERNS = [
    /^\s*\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s+(DEBUG|TRACE|INFO|NOTICE|WARNING|WARN|ERROR|CRITICAL|FATAL)\b/,
    /^\s*\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:\s*[+-]\d{2}:?\d{2}|Z)?\s+-\s+\S+(?:\s+\S+)*?\s+-\s+(DEBUG|TRACE|INFO|NOTICE|WARNING|WARN|ERROR|CRITICAL|FATAL)\s+-/,
    /^\s*\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:\s*[+-]\d{2}:?\d{2}|Z)?\s+\[?(DEBUG|TRACE|INFO|NOTICE|WARNING|WARN|ERROR|CRITICAL|FATAL)\]?\b/,
    /^\s*\[?(DEBUG|TRACE|INFO|NOTICE|WARNING|WARN|ERROR|CRITICAL|FATAL)\]?(?:\s*:|\s+-|\s{2,})/,
    /^\s*\[(debug|trace|info|notice|warning|warn|error|critical|fatal)\](?:\s|$|:)/i,
    /(?:^|[\s,{])level=["']?(debug|trace|info|notice|warn|warning|error|critical|fatal)["']?\b/i,
  ];

  const BENIGN_ZERO_SUMMARY_RE = /^\s*(?:[│|]\s*)?(?:(?:total\s+)?(?:error(?:s|\s+records?|\s+count|\s+rate)?|warning(?:s|\s+count)?|failed(?:\s+requests?)?)\s*[│|:=][│|:=\s]*0(?:\.0+)?%?|0\s+(?:errors?|warnings?|failed(?:\s+requests?)?|error\s+records?))(?=$|[\s│|])(?:\s*[│|])*\s*$/i;
  const CLI_COMMAND_LINE_RE = /^\s*CLI Command\s*:/i;
  const CLI_CONFIG_LOG_RE = /^(?:\s*CLI Command\s*:|\s*(?:error_rate_slo|request_timeout|early_stop_error_threshold)\s*[:=])/i;
  const UNSTRUCTURED_WARNING_RE = /^\s*(?:Warning|[A-Za-z_]\w*Warning)\s*:/i;
  const TRACEBACK_START_RE = /Traceback \(most recent call last\):/;
  const TRACEBACK_CHAIN_RE = /(?:During handling of the above exception|The above exception was the direct cause)/;
  const TRACEBACK_CHAIN_LINE_RE = /^\s*(?:During handling of the above exception, another exception occurred:|The above exception was the direct cause of the following exception:)\s*$/;
  const TRACEBACK_END_RE = /^[A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit)\b/;

  const UNSTRUCTURED_ERROR_PATTERNS = [
    TRACEBACK_START_RE,
    TRACEBACK_CHAIN_LINE_RE,
    /(?:^|[\s;|])(?:Error|Exception|[A-Za-z_][\w.]*(?:Error|Exception))\s*:/,
    /\b(?:FATAL|CRITICAL|Segmentation fault|Killed|OOMKilled)\b/,
    /\b(?:connection refused|connection reset|timed out|read timeout|connect timeout|request timed out)\b/i,
    /\bexited with (?:non-zero )?(?:code|status)\s+[1-9]\d*\b/i,
    /\bHTTP\/\d(?:\.\d)?["'\s]+[45]\d\d\b/i,
    /\bstatus(?:_code)?\s*[=:]\s*[45]\d\d\b/i,
    /^\s*Task failed\b/i,
    /\b(?:subprocess|runner|benchmark)\s+failed\b/i,
  ];

  function stripAnsi(line) {
    return String(line ?? "").replace(LOG_ANSI_RE, "");
  }

  function matchExplicitLogLevel(text) {
    for (let i = 0; i < EXPLICIT_LOG_LEVEL_PATTERNS.length; i += 1) {
      const m = EXPLICIT_LOG_LEVEL_PATTERNS[i].exec(text);
      if (m) {
        const lvl = m[1].toUpperCase();
        if (lvl === "ERROR" || lvl === "CRITICAL" || lvl === "FATAL") return "error";
        if (lvl === "WARNING" || lvl === "WARN") return "warning";
        return "info";
      }
    }
    return null;
  }

  function classifyUnstructuredLogLine(text) {
    if (BENIGN_ZERO_SUMMARY_RE.test(text) || CLI_CONFIG_LOG_RE.test(text)) {
      return "info";
    }
    if (UNSTRUCTURED_WARNING_RE.test(text)) {
      return "warning";
    }
    for (const re of UNSTRUCTURED_ERROR_PATTERNS) {
      if (re.test(text)) return "error";
    }
    return "info";
  }

  // eslint-disable-next-line no-unused-vars -- exported for unit tests in test_report_builder.py
  function classifyLogLine(rawLine) {
    const text = stripAnsi(rawLine);
    if (CLI_COMMAND_LINE_RE.test(text)) {
      return "info";
    }
    const explicit = matchExplicitLogLevel(text);
    if (TRACEBACK_START_RE.test(text)) {
      return explicit === "info" ? "info" : "error";
    }
    return explicit !== null ? explicit : classifyUnstructuredLogLine(text);
  }

  function classifyLogLines(lines) {
    if (!Array.isArray(lines) || !lines.length) return [];
    const out = [];
    let inTraceback = false;
    for (const rawLine of lines) {
      const text = stripAnsi(rawLine);
      const isCliCommand = CLI_COMMAND_LINE_RE.test(text);
      const explicit = isCliCommand ? null : matchExplicitLogLevel(text);
      if (inTraceback) {
        if (explicit !== null) {
          inTraceback = false;
        } else if (/^\s+\S/.test(text) || TRACEBACK_CHAIN_RE.test(text)) {
          out.push({ text, severity: "error" });
          continue;
        } else if (TRACEBACK_END_RE.test(text)) {
          inTraceback = false;
          out.push({ text, severity: "error" });
          continue;
        } else {
          inTraceback = false;
        }
      }

      if (isCliCommand) {
        out.push({ text, severity: "info" });
        continue;
      }

      if (TRACEBACK_START_RE.test(text)) {
        if (explicit === "info") {
          out.push({ text, severity: "info" });
        } else {
          inTraceback = true;
          out.push({ text, severity: "error" });
        }
        continue;
      }

      out.push({ text, severity: explicit !== null ? explicit : classifyUnstructuredLogLine(text) });
    }
    return out;
  }

  function isLogErrorOrWarning(severity) {
    return severity === "error" || severity === "warning";
  }

  function renderConfigLogs(pageNode) {
    const d = state.data;
    const manifest = d.run_manifest || {};
    const pre = el("pre", { className: "log-box", textContent: JSON.stringify(manifest, null, 2) });
    pageNode.appendChild(makeSection("c-manifest", "Reproducible Run Manifest (Redacted)", "Credentials and secret values removed", pre));

    const logsWrap = el("div");
    const searchInput = el("input", { className: "input", type: "search", placeholder: "Filter logs...", value: state.logSearch });
    const errBtn = el("button", { type: "button", className: `pill-btn${state.logErrorsOnly ? " active" : ""}`, textContent: "Errors / Warnings Only" });
    const logPre = el("pre", { className: "log-box" });

    const allLines = []
      .concat((d.logs_snapshot && d.logs_snapshot.lifecycle) || [])
      .concat((d.logs_snapshot && d.logs_snapshot.stdout) || [])
      .concat((d.logs_snapshot && d.logs_snapshot.stderr) || []);
    const classifiedLines = classifyLogLines(allLines);

    function updateLogs() {
      const q = state.logSearch.trim().toLowerCase();
      const filtered = classifiedLines.filter((entry) => {
        if (state.logErrorsOnly && !isLogErrorOrWarning(entry.severity)) return false;
        if (q && !entry.text.toLowerCase().includes(q)) return false;
        return true;
      });
      if (!filtered.length) {
        logPre.replaceChildren(document.createTextNode("No matching log lines."));
        return;
      }
      const frag = typeof document.createDocumentFragment === "function" ? document.createDocumentFragment() : null;
      const nodes = frag ? null : [];
      const pushNode = (node) => {
        if (frag) frag.appendChild(node);
        else nodes.push(node);
      };
      for (let i = 0; i < filtered.length; i += 1) {
        if (i > 0) pushNode(document.createTextNode("\n"));
        const entry = filtered[i];
        if (entry.severity === "error") {
          pushNode(el("span", { className: "log-line-err", textContent: entry.text }));
        } else if (entry.severity === "warning") {
          pushNode(el("span", { className: "log-line-warn", textContent: entry.text }));
        } else {
          pushNode(document.createTextNode(entry.text));
        }
      }
      if (frag) logPre.replaceChildren(frag);
      else logPre.replaceChildren(...nodes);
    }
    searchInput.addEventListener("input", (e) => {
      state.logSearch = e.target.value;
      updateLogs();
    });
    errBtn.addEventListener("click", () => {
      state.logErrorsOnly = !state.logErrorsOnly;
      errBtn.classList.toggle("active", state.logErrorsOnly);
      updateLogs();
    });
    updateLogs();
    logsWrap.append(el("div", { className: "filter-bar" }, [searchInput, errBtn]), logPre);
    pageNode.appendChild(makeSection("c-logs", "Logs Snapshot", "Redacted task lifecycle and runner output", logsWrap));
  }

  function kpiTile(label, val, sub, toneOrOpts, helpText, helpFormula) {
    let tone = null;
    let explicitHelp = helpText;
    let explicitFormula = helpFormula;
    if (toneOrOpts && typeof toneOrOpts === "object") {
      tone = toneOrOpts.tone || null;
      explicitHelp = toneOrOpts.helpText || explicitHelp;
      explicitFormula = toneOrOpts.helpFormula || toneOrOpts.formula || explicitFormula;
    } else if (typeof toneOrOpts === "string") {
      if (["good", "warn", "crit", "info"].includes(toneOrOpts)) {
        tone = toneOrOpts;
      } else if (!explicitHelp) {
        explicitHelp = toneOrOpts;
      }
    }
    const cls = tone ? `kpi ${tone}` : "kpi";
    return el("div", { className: cls }, [
      el("div", { className: "kpi-label" }, [
        el("span", { textContent: label }),
        helpBadge(label, explicitHelp, explicitFormula),
      ]),
      el("div", { className: "kpi-val", textContent: val }),
      sub ? el("div", { className: "kpi-sub", textContent: sub }) : null,
    ]);
  }

  function renderAllPages() {
    cleanupChartMountsIn(null);
    state.crosshairSubscribers = [];
    const root = document.getElementById("report-root");
    const pages = [
      { id: "overview", render: renderOverview },
      { id: "workload", render: renderWorkload },
      { id: "bottlenecks", render: renderBottlenecks },
      { id: "serving", render: renderServing },
      { id: "requests", render: renderRequests },
      { id: "system", render: renderSystem },
      { id: "hardware", render: renderHardware },
      { id: "config", render: renderConfigLogs },
    ];
    const nodes = pages.map((p) => {
      const div = el("div", { className: `report-page${state.page === p.id ? " active" : ""}`, "data-page": p.id });
      p.render(div);
      return div;
    });
    root.replaceChildren(...nodes);
    scheduleReflow(root);
  }

  function switchPage(pageId) {
    state.page = pageId;
    let activePage = null;
    document.querySelectorAll(".report-page").forEach((n) => {
      const isTarget = n.getAttribute("data-page") === pageId;
      n.classList.toggle("active", isTarget);
      if (isTarget) activePage = n;
    });
    document.querySelectorAll(".nav-btn").forEach((b) => b.classList.toggle("active", b.getAttribute("data-page") === pageId));
    document.querySelectorAll(".nav-group").forEach((g) => g.classList.toggle("open", g.getAttribute("data-page") === pageId));
    scheduleReflow(activePage || document.getElementById("report-root"));
  }

  function renderHeader() {
    const t = (state.data && state.data.task) || {};
    const f = (state.data && state.data.forensics) || {};
    document.getElementById("report-title").textContent = `${t.name || "Simulation"} (${t.id || ""})`;
    document.getElementById("report-meta").textContent = `${t.backend || ""} ${t.backend_version || ""} · model: ${t.model_name || ""} · duration: ${secFmt(t.duration_seconds)} · warmup: ${secFmt(t.warmup_seconds || 0)} · scale: ${t.scale_factor || 1}x`;
    const badges = [el("span", { className: "badge info", textContent: t.status || "completed" })];
    if (t.artifacts_incomplete || f.partial) badges.push(el("span", { className: "badge warn", textContent: "Partial Results" }));
    if (f.telemetry_scope) badges.push(el("span", { className: "badge", textContent: f.telemetry_scope }));
    document.getElementById("status-badges").replaceChildren(...badges);
  }

  async function loadEmbeddedData() {
    const raw = document.getElementById("lens-report-data").textContent.trim();
    const parsed = JSON.parse(raw);
    if (parsed && parsed.compressed === "gzip-base64" && typeof parsed.payload === "string") {
      const bin = atob(parsed.payload);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
      const text = await new Response(stream).text();
      return JSON.parse(text);
    }
    return parsed;
  }

  async function init() {
    state.data = await loadEmbeddedData();
    renderHeader();
    renderAllPages();

    const sidebarEl = document.getElementById("sidebar");
    document.getElementById("sidebar-toggle").addEventListener("click", () => {
      if (sidebarEl) sidebarEl.classList.toggle("collapsed");
      scheduleReflow(null);
    });
    if (sidebarEl && typeof sidebarEl.addEventListener === "function") {
      sidebarEl.addEventListener("transitionend", (e) => {
        if (!e || !e.propertyName || e.propertyName === "width") {
          scheduleReflow(null);
        }
      });
    }
    if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
      let resizeTimer = null;
      window.addEventListener("resize", () => {
        if (typeof setTimeout === "function") {
          if (resizeTimer !== null && typeof clearTimeout === "function") {
            clearTimeout(resizeTimer);
          }
          resizeTimer = setTimeout(() => {
            resizeTimer = null;
            scheduleReflow(null);
          }, 100);
        } else {
          scheduleReflow(null);
        }
      });
      window.addEventListener("beforeprint", () => {
        reflowVisibleCharts(null);
      });
      window.addEventListener("afterprint", () => {
        scheduleReflow(null);
      });
      if (typeof window.matchMedia === "function") {
        const printMql = window.matchMedia("print");
        if (printMql && typeof printMql.addEventListener === "function") {
          printMql.addEventListener("change", () => reflowVisibleCharts(null));
        }
      }
    }
    document.getElementById("theme-toggle").addEventListener("click", (e) => {
      state.theme = state.theme === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", state.theme);
      e.target.textContent = state.theme === "dark" ? "Light Mode" : "Dark Mode";
      renderAllPages();
    });
    document.getElementById("print-btn").addEventListener("click", () => window.print());
    document.getElementById("window-toggle").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-window]");
      if (!btn) return;
      state.windowMode = btn.getAttribute("data-window");
      document.querySelectorAll("#window-toggle button").forEach((b) => b.classList.toggle("active", b === btn));
      renderAllPages();
    });
    document.getElementById("nav-list").addEventListener("click", (e) => {
      const btn = e.target.closest(".nav-btn[data-page]");
      if (btn) {
        switchPage(btn.getAttribute("data-page"));
        return;
      }
      const link = e.target.closest(".subnav a[data-page]");
      if (link) {
        switchPage(link.getAttribute("data-page"));
      }
    });
  }

  init().catch((err) => {
    const root = document.getElementById("report-root");
    root.replaceChildren(el("div", { className: "empty-note", textContent: `Failed to initialize offline report: ${err && err.message ? err.message : err}` }));
  });
})();
