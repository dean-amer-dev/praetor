# Phase 13 — Infrastructure Stabilization

**Goal:** Fix three overlapping infrastructure failures that are blocking agents from running correctly: arch mismatches taking down mem0 and prometheus, missing CI for qa/reviewer worker images, and runner consolidation cleanup.

This phase unblocks Phase 4 (Memory), Phase 7 (PR Reviewer + QA), and Phase 9 (Observability).

---

## 13a — mem0 Arch Fix

**Root cause:** `amerenda/mem0-server:latest` is amd64-only. Pod scheduled on `rpi5-0` (arm64) because no nodeAffinity is set. Crash: `exec /usr/bin/sh: exec format error`.

**Quick fix (immediate):**
Add nodeAffinity to `k3s-dean-gitops/apps/mem0/server/deployment.yaml` to pin pod to amd64 nodes:

```yaml
affinity:
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values:
                - murderbot
                - archlinux
```

**Proper fix (follow-up):**
Build multi-arch image in mem0 CI:
```yaml
- uses: docker/build-push-action@v5
  with:
    platforms: linux/amd64,linux/arm64
    push: true
    tags: amerenda/mem0-server:latest
```

**Done conditions:**
- [ ] mem0 pod Running on amd64 node
- [ ] `curl https://mem0.amer.dev/healthz` returns 200
- [ ] Phase 4 status → ✅ Complete

---

## 13b — Praetor qa-worker and reviewer-worker Images

**Root cause:** `amerenda/praetor-qa:latest` and `amerenda/praetor-reviewer:latest` have never been built or pushed to DockerHub. Dockerfiles exist in the praetor repo but were never wired into CI.

**Working CI reference:** Look at how `praetor-coder-worker`, `praetor-research-worker`, etc. are built — those pods are Running, so their CI is working. Extend the same workflow to cover qa and reviewer.

**Dockerfiles to build:**
- `Dockerfile.qa-worker` → `amerenda/praetor-qa:latest`
- `Dockerfile.reviewer-worker` → `amerenda/praetor-reviewer:latest`

**Build requirements:**
- Must be multi-arch: `linux/amd64,linux/arm64`
- Same base image and tooling as other workers
- Push on merge to main

**Done conditions:**
- [ ] Both images exist on DockerHub with multi-arch manifest
- [ ] `praetor-qa-worker` pod Running (was: ImagePullBackOff)
- [ ] `praetor-reviewer-worker` pod Running (was: ImagePullBackOff)
- [ ] `app-praetor-qa-worker` ArgoCD app: Synced + Healthy
- [ ] `app-praetor-reviewer-worker` ArgoCD app: Synced + Healthy

**Additional:** Once workers are running, complete Phase 7 by registering the GitHub org webhook (see Phase 7 Blocker section in status.md).

---

## 13c — Prometheus Crash Loop

**Root cause:** `prometheus-infra-monitoring-kube-prom-prometheus-0` scheduled on `rpi5-0` (arm64 RPi, low memory). 182 WAL segments to replay; one segment took 3.99s vs normal ~100µs. OOM during WAL replay causes crash before prometheus binds its port, so config-reloader times out trying to hit `/-/reload`.

**Quick fix:**
Add nodeAffinity to the kube-prometheus-stack values to pin prometheus to amd64 nodes.

In `k3s-dean-gitops/infra/monitoring/values.yaml` (or equivalent prometheus operator values):

```yaml
prometheus:
  prometheusSpec:
    nodeSelector:
      kubernetes.io/hostname: murderbot
    # or use affinity for more flexibility:
    affinity:
      nodeAffinity:
        requiredDuringSchedulingIgnoredDuringExecution:
          nodeSelectorTerms:
            - matchExpressions:
                - key: kubernetes.io/hostname
                  operator: In
                  values:
                    - murderbot
                    - archlinux
```

**If WAL is corrupted / too large to replay:**
```bash
# As last resort — deletes ~2h of metrics, prometheus starts clean
kubectl exec -n monitoring prometheus-infra-monitoring-kube-prom-prometheus-0 \
  -c prometheus -- rm -rf /prometheus/wal/*
# Then delete the pod to force restart
kubectl delete pod -n monitoring prometheus-infra-monitoring-kube-prom-prometheus-0
```

**Done conditions:**
- [ ] prometheus pod 2/2 Running on amd64 node
- [ ] `infra-monitoring` ArgoCD app: Synced + Healthy
- [ ] Prometheus accessible at `https://prometheus.amer.dev` (or internal URL)
- [ ] No alertmanager alerts for prometheus down

---

## 13d — ARC Runner IgnoreExtraneous (Belt and Suspenders)

**Root cause:** Stash merge conflict left `root-app.yaml` with unresolved markers. Conflict resolved 2026-06-13, PR #747 open.

**What was fixed:** All 8 runner ArgoCD apps now have `IgnoreExtraneous=true` in syncOptions. Note: the real fix was `application.resourceTrackingMethod: annotation` (PR #744) — all runners were already Synced+Healthy before #747. This PR is belt-and-suspenders.

**Done conditions:**
- [ ] PR #747 merged: https://github.com/amerenda/k3s-dean-gitops/pull/747
- [ ] All 8 runner apps Synced + Healthy (already true, merge is formality)

---

## Pre-conditions for Phase 14+

Phase 13 must be fully complete before resuming:
- Phase 9 (Observability): needs prometheus running (13c) + agents healthy (13a, 13b)
- Phase 10 (MCP Gateway): needs all agents healthy
- Phase 11 (Scaffold Worker): needs infra-mcp validated end-to-end (see cleanup plan Phase 6)
