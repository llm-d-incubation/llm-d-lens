import React from 'react';
import { ArrowRight, ChevronRight } from 'lucide-react';
import { Button } from '../../../shared/ui/Button';
import { Panel } from '../../../shared/ui/Panel';
import { useAuth } from '../../auth';
import { visibleWelcomeActions } from '../model/quickActions';

export function QuickActions({ onNavigate, onCollapse }) {
    const { can, canView } = useAuth();
    const visible = visibleWelcomeActions(can, canView);
    if (!visible.length) return null;
    return (
        <Panel
            title="Quick actions"
            className="flex flex-col bg-transparent shadow-none backdrop-blur-none not-dark:border-[#cbd5e1]"
            actions={onCollapse ? (
                <Button
                    variant="ghost"
                    size="icon"
                    onClick={onCollapse}
                    aria-label="Collapse quick actions"
                    title="Collapse quick actions"
                >
                    <ChevronRight size={14} aria-hidden="true" />
                </Button>
            ) : undefined}
        >
            <div className="space-y-2">
                {visible.map((action) => {
                    const Icon = action.icon;
                    return (
                        <Button
                            key={action.id}
                            variant="secondary"
                            size="md"
                            onClick={() => onNavigate(action.view, action.params || {})}
                            className="h-auto w-full items-start justify-start gap-3 whitespace-normal border-[#cbd5e1] bg-white/50 py-3 text-left hover:border-[#94a3b8] hover:bg-sky-500/10 dark:border-sky-500/20 dark:bg-slate-900/35 dark:hover:bg-sky-500/10"
                        >
                            <span className="mt-0.5 rounded-lg bg-[#037ffc1a] p-1.5 text-[#037ffc] dark:bg-sky-500/15 dark:text-sky-300">
                                <Icon size={16} aria-hidden="true" />
                            </span>
                            <span className="min-w-0 flex-1">
                                <span className="block text-sm font-semibold text-theme-text">{action.label}</span>
                                <span className="mt-0.5 block text-[11px] font-normal leading-relaxed text-theme-muted">
                                    {action.description}
                                </span>
                            </span>
                            <ArrowRight size={14} className="mt-0.5 shrink-0 text-theme-muted" aria-hidden="true" />
                        </Button>
                    );
                })}
            </div>
        </Panel>
    );
}
