import React from 'react';
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronUp, Clock, Info, ShieldAlert } from 'lucide-react';
import { Badge } from '../../../shared/ui/Badge';
import { Button } from '../../../shared/ui/Button';
import { cn } from '../../../shared/lib/cn';
import { EmptyState } from '../../../shared/ui/EmptyState';
import { useAuth } from '../../auth';
import {
    actionLabel,
    evidenceRows,
    formatAge,
    severityTone,
} from '../model/welcomePresentation';

const SEVERITY_ICON = {
    critical: ShieldAlert,
    warning: AlertTriangle,
    info: Info,
};

function canUseAction(action, canView, can) {
    if (!action?.view) return false;
    if (!canView(action.view)) return false;
    return !action.permission || can(action.permission);
}

function IssueActions({ issue, onNavigate, canView, can }) {
    const actions = [issue.primaryAction, issue.secondaryAction]
        .filter((action) => canUseAction(action, canView, can));
    if (!actions.length) return null;
    return (
        <div className="flex flex-wrap gap-2">
            {actions.map((action) => (
                <Button
                    key={`${action.view}:${action.intent}`}
                    variant={action === issue.primaryAction ? 'secondary' : 'ghost'}
                    size="xs"
                    className="w-20 not-dark:border-[#d1d5db] not-dark:bg-[#f3f4f6] not-dark:text-[#4b5563] not-dark:hover:border-[#9ca3af] not-dark:hover:bg-[#e5e7eb] not-dark:hover:text-[#374151]"
                    onClick={() => onNavigate?.(action.view, {
                        ...(issue.clusterId ? { clusterId: issue.clusterId } : {}),
                        ...action.params,
                    })}
                >
                    {actionLabel(action.intent)}
                </Button>
            ))}
        </div>
    );
}

export function IssueRow({ issue, onNavigate, expandable = false, expanded = false, onToggle }) {
    const { canView, can } = useAuth();
    const SeverityIcon = SEVERITY_ICON[issue.severity] || Info;
    const rows = evidenceRows(issue, expandable && expanded ? 8 : 2);
    const scope = issue.code === 'cluster.none_available'
        ? 'No clusters'
        : issue.code === 'ai-provider.none_configured'
            ? 'Global'
            : issue.clusterName || issue.clusterId || 'All clusters';
    const scopeLabel = issue.clusterId || issue.clusterName ? 'Cluster' : 'Scope';
    return (
        <article className="rounded-lg border border-[#b7d4e6] bg-white px-3 py-2.5 shadow-sky-500/5 backdrop-blur-sm transition-colors hover:border-[#9fc0d6] dark:border-sky-500/20 dark:shadow-sm dark:bg-transparent dark:bg-gradient-to-br dark:from-slate-900/80 dark:via-slate-900/50 dark:to-sky-950/25 dark:hover:border-sky-500/25 dark:hover:from-slate-900/90 dark:hover:via-slate-900/60 dark:hover:to-sky-950/35">
            <div className="flex items-start gap-2.5">
                <div className="flex shrink-0 items-start gap-2.5">
                    <Badge
                        tone={severityTone(issue.severity)}
                        size="xs"
                        className={cn('severity-badge mt-0.5 w-20 shrink-0 justify-center gap-1', `severity-badge-${issue.severity}`)}
                    >
                        <SeverityIcon size={11} aria-hidden="true" />
                        {issue.severity}
                    </Badge>
                    <span className="mt-1 h-8 w-px bg-theme-border/70" aria-hidden="true" />
                </div>
                <div className="min-w-0 flex-1">
                    <div className="flex items-start justify-between gap-3">
                        <h4 className="truncate text-[13px] font-semibold leading-snug tracking-[-0.01em] text-theme-text" title={issue.title}>
                            {issue.title}
                        </h4>
                        {(issue.firstSeenAt || issue.observedAt) && (
                            <span className="inline-flex shrink-0 items-center gap-1 text-[9px] font-medium text-theme-muted">
                                <Clock size={10} className="opacity-70" aria-hidden="true" />
                                <span className="font-semibold text-theme-text">
                                    {formatAge(issue.firstSeenAt || issue.observedAt)}
                                </span>
                            </span>
                        )}
                    </div>
                    <p className="mt-0.5 line-clamp-2 text-[11px] leading-snug text-theme-muted">{issue.message}</p>
                    <div className="mt-1.5 flex items-center justify-between gap-2">
                        <div className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-2.5 gap-y-1 text-[10px] text-theme-muted sm:flex-nowrap sm:overflow-hidden">
                            <span
                                className="inline-flex min-w-0 items-baseline gap-1"
                                title={`${scopeLabel}: ${scope}`}
                            >
                                <span className="shrink-0 text-[9px] font-semibold uppercase tracking-wide">
                                    {scopeLabel}
                                </span>
                                <span className="truncate text-theme-text">{scope}</span>
                            </span>
                            {issue.stale && (
                                <span className="shrink-0 font-semibold text-amber-700 dark:text-amber-300">Stale</span>
                            )}
                            {issue.coverage?.expected ? (
                                <span className="shrink-0">
                                    Coverage{' '}
                                    <span className="font-semibold text-theme-text">
                                        {issue.coverage.observed ?? 0}/{issue.coverage.expected} {issue.coverage.unit}
                                    </span>
                                </span>
                            ) : null}
                            {rows.map((row) => (
                                <span
                                    key={row.key}
                                    className="inline-flex min-w-0 items-baseline gap-1"
                                    title={`${row.label}: ${row.value}`}
                                >
                                    <span className="shrink-0 text-[9px] font-semibold uppercase tracking-wide">
                                        {row.label}
                                    </span>
                                    <span className="truncate font-mono text-[10px] font-semibold text-theme-text">
                                        {row.value}
                                    </span>
                                </span>
                            ))}
                        </div>
                        <div className="flex shrink-0 items-center gap-1">
                            <IssueActions issue={issue} onNavigate={onNavigate} canView={canView} can={can} />
                            {expandable && (
                                <Button variant="ghost" size="xs" onClick={() => onToggle?.(issue.id)}>
                                    {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
                                    {expanded ? 'Hide evidence' : 'Evidence'}
                                </Button>
                            )}
                        </div>
                    </div>
                </div>
            </div>
        </article>
    );
}

export function IssueList({
    issues,
    onNavigate,
    emptyTitle = 'All systems operational',
    emptyMessage = 'Health checks and telemetry across monitored clusters are reporting normally. No action is required.',
    expandable = false,
    expandedIds = new Set(),
    onToggle,
    limit,
}) {
    const visible = limit ? issues.slice(0, limit) : issues;
    if (!visible.length) {
        return (
            <EmptyState
                icon={<CheckCircle2 size={48} strokeWidth={1.6} />}
                title={emptyTitle}
                message={emptyMessage}
                tone="success"
                variant="status"
            />
        );
    }
    return (
        <div className="space-y-2">
            {visible.map((issue) => (
                <IssueRow
                    key={issue.id}
                    issue={issue}
                    onNavigate={onNavigate}
                    expandable={expandable}
                    expanded={expandedIds.has(issue.id)}
                    onToggle={onToggle}
                />
            ))}
        </div>
    );
}
