# llm-d Lens Roadmap

| Category / Milestone | v0.1.0-dev<br>*llm-d-incubation* | v0.1.0<br>🟢 In Progress<br>*llm-d Core*  | v0.2.0<br>*Multi-HW Expansion* | v0.?.?<br>*Advanced Engine & Planning* |
| --- | --- | --- | --- | --- |
| **Planned Release Date** | Oct 3, 2026 | Oct 17, 2026 | `TBD` | `TBD` |
| **System Stack & Hardware Matrix** | | | | |
| llm-d Stack Version Target | `v0.9.0` | `v0.10.0` | `v0.11.0` | `TBD / Future Release` |
| Hardware Architecture Support | Intel XPU | Intel XPU, Nvidia GPU (Basic) | Intel XPU, Nvidia GPU, CPU | Intel XPU, Nvidia GPU, AMD Instinct GPU, Google TPU, CPU<br>Heterogenous support |
| OS | Ubuntu 22.04, Ubuntu 24.04 | Ubuntu 22.04, Ubuntu 24.04 | Ubuntu 22.04, Ubuntu 24.04 | Ubuntu 22.04, Ubuntu 24.04, Debian 12, Debian 13, RHEL 9, RHEL 10, Windows, macOS |
| Inference Engine Integration | vLLM | vLLM | vLLM | vLLM, vLLM-omni, SGLang |
| **1. Deployment & Cluster Orchestration** | | | | |
| Template-Based Deployment | XPU Deployment Templates:<br>• optimized-baseline<br>• pd-disaggregation<br>• precise-prefix-cache-routing<br>• tiered-prefix-cache | Nvidia GPU Deployment Templates:<br>• optimized-baseline<br>• pd-disaggregation<br>• precise-prefix-cache-routing<br>• tiered-prefix-cache | Deployment Templates:<br>• predicted-latency-routing<br>• workload-autoscaling | Deployment Templates:<br>• `TBD`<br>Features:<br>• Heterogenous Deployment<br>• Air-Gapped Deployment |
| Agentic Deployment | Agentic Deployment on XPU | — | Agentic Config Recommendation (Performance-History-Driven) | • Agentic Config Recommendation for Custom Deployment Topologies<br>• Add BLIS as one of the performance projection backends<br>• Capacity Planning via BLIS |
| Custom Deployment | N/A | N/A | Parametric Deployment (Composable llm-d Performance Features) | • Heterogenous Deployment<br>• Air-Gapped Deployment |
| Cluster Scaling & Orchestration | — | Nvidia GPU Cluster Integration | • XPU Auto-Scaling Support<br>• Multi-cluster llm-d *(pending on [llm-d-router#1907](https://github.com/llm-d/llm-d-router/issues/1907))*<br>• Non-Kubernetes Cluster Administration Support | — |
| **2. Benchmarking & Profiling** | | | | |
| Evaluation | Inference Perf (llm-d-benchmark) | — | — | Accuracy Benchmark Suite |
| Profiling | Request/s and Token/s Flowmap | — | — | Agentic Profiling |
| Simulation (Trace Replayer) | Trace Replayer (ai-perf, trace-replayer) | — | — | TCO & Power Consumption |
| **3. Observability, Diagnostics & Telemetry** | | | | |
| Telemetry & Metrics | XPU Telemetry | Nvidia GPU Telemetry | — | — |
| Interconnect & Hardware Diagnostics | N/A | N/A | Network & PD Diagnostics (NIXL / RDMA via @llm-d-pd-utils) | — |
| **4. Enterprise Platform & Governance** | | | | |
| Model & Asset Management | Model Market, Storage Management, Model Cache Management | — | Model Catalog (Inference Engine × Hardware Compatibility Matrix) | — |
| User Service & Token Management | Model Services, API Keys & Usage Tracking | — | — | Token quota |
| Platform Security & AuthN/AuthZ | Password AuthN, LDAP / IdP Integration, RBAC | Master Key Secrets Management | • Secure Serving<br>• SSO via OIDC | • SCIM Provisioning |
| Extensibility & Usability | AI Assistant Interactivity, System Audit Logging | Pluggable Hardware Abstraction | CLI Support | • Pluggable Inference Engine Abstraction |

---

"—" indicates no planned work for that milestone in this category; "N/A" indicates the capability does not yet exist at that milestone. Versions and scope are indicative and may shift as upstream llm-d stack releases land.
