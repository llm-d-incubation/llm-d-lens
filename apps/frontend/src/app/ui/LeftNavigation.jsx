import React, { useState, useEffect } from 'react';
import { Database, Webcam, Rocket, Gauge, Network, HardDrive, Download, Plug, Bot, ChevronDown,
    Users, UserCog, ShieldCheck, KeyRound, Monitor, ScrollText, Menu, Boxes, Activity, Waypoints,
    Sparkles, Fingerprint, Sun, Moon
} from 'lucide-react';
import { Button } from '../../shared/ui/Button';
import { cn } from '../../shared/lib/cn';
import { SimulationIcon } from '../../pages/simulation';
import { ChangePasswordModal, useAuth } from '../../pages/auth';
import { NAVIGATION_GROUPS } from '../routes/views';

const MENU_ICONS = {
    Database, Webcam, Rocket, Gauge, Network, HardDrive, Download, Plug, Bot,
    Users, UserCog, ShieldCheck, KeyRound, Monitor, ScrollText, Boxes, Activity,
    Waypoints, SimulationIcon, Sparkles, Fingerprint,
};

// Groups that start collapsed. Everything else opens with the sidebar.
const DEFAULT_COLLAPSED_GROUPS = [];
const GROUP_COLLAPSE_KEY = 'prism_nav_collapsed_groups';

const ITEM_THEMES = {
    'optimization-deployments': {
        activeBg: 'bg-gradient-to-r from-cyan-50 via-sky-50/70 to-white border-cyan-300/60 text-cyan-800 dark:from-cyan-950/20 dark:via-sky-950/10 dark:to-slate-950/20 dark:border-cyan-500/20 dark:text-cyan-300',
        activeIcon: 'bg-gradient-to-br from-cyan-500 to-sky-500 text-white shadow-[0_0_15px_rgba(6,182,212,0.35)]',
        indicator: 'bg-gradient-to-b from-cyan-400 to-sky-500 shadow-[0_0_8px_rgba(6,182,212,0.6)]'
    },
    'optimization-workspace': {
        activeBg: 'bg-gradient-to-r from-emerald-50 via-teal-50/70 to-white border-emerald-300/60 text-emerald-800 dark:from-emerald-950/20 dark:via-teal-950/10 dark:to-slate-950/20 dark:border-emerald-500/20 dark:text-emerald-300',
        activeIcon: 'bg-gradient-to-br from-emerald-500 to-teal-500 text-white shadow-[0_0_15px_rgba(16,185,129,0.35)]',
        indicator: 'bg-gradient-to-b from-emerald-400 to-teal-500 shadow-[0_0_8px_rgba(16,185,129,0.6)]'
    },
    'opt-simulation': {
        activeBg: 'bg-gradient-to-r from-violet-50 via-fuchsia-50/70 to-white border-violet-300/60 text-violet-800 dark:from-violet-950/20 dark:via-fuchsia-950/10 dark:to-slate-950/20 dark:border-violet-500/20 dark:text-violet-300',
        activeIcon: 'bg-gradient-to-br from-violet-500 to-fuchsia-500 text-white shadow-[0_0_15px_rgba(139,92,246,0.35)]',
        indicator: 'bg-gradient-to-b from-violet-400 to-fuchsia-500 shadow-[0_0_8px_rgba(139,92,246,0.6)]'
    }
};

export default function LeftNavigation({ currentView, onNavigate, isMobileOpen, isExpanded: controlledExpanded, onExpandedChange, theme = 'dark', onToggleTheme }) {
    const { canView, principal, logout } = useAuth();
    const [accountMenuOpen, setAccountMenuOpen] = useState(false);
    const [showChangePassword, setShowChangePassword] = useState(false);
    const [uncontrolledExpanded, setUncontrolledExpanded] = useState(() => {
        const saved = localStorage.getItem('prism_sidebar_expanded');
        return saved !== null ? saved === 'true' : true;
    });
    const isExpanded = controlledExpanded ?? uncontrolledExpanded;
    const setIsExpanded = (nextExpanded) => {
        const nextValue = typeof nextExpanded === 'function' ? nextExpanded(isExpanded) : nextExpanded;
        if (controlledExpanded === undefined) setUncontrolledExpanded(nextValue);
        onExpandedChange?.(nextValue);
    };

    const [collapsedGroups, setCollapsedGroups] = useState(() => {
        try {
            const saved = JSON.parse(localStorage.getItem(GROUP_COLLAPSE_KEY));
            if (Array.isArray(saved)) return new Set(saved);
        } catch {
            // Ignore malformed preferences and fall back to the defaults.
        }
        return new Set(DEFAULT_COLLAPSED_GROUPS);
    });

    useEffect(() => {
        localStorage.setItem('prism_sidebar_expanded', isExpanded);
    }, [isExpanded]);

    useEffect(() => {
        localStorage.setItem(GROUP_COLLAPSE_KEY, JSON.stringify([...collapsedGroups]));
    }, [collapsedGroups]);

    const toggleGroup = (groupId) => {
        setCollapsedGroups((current) => {
            const next = new Set(current);
            if (next.has(groupId)) next.delete(groupId);
            else next.add(groupId);
            return next;
        });
    };

    const handleItemClick = (view, disabled) => {
        if (!disabled) {
            onNavigate(view);
        }
    };

    const visibleGroups = NAVIGATION_GROUPS
        .map(group => ({
        ...group,
        items: group.items.filter(item => !item.disabled && canView(item.view))
    })).filter(group => group.items.length > 0);

    return (
        <>
        <aside className={cn(
            'lens-navigation fixed top-20 left-4 h-[calc(100vh-6rem)]',
            isMobileOpen ? 'flex' : 'hidden md:flex',
            'flex-col border-0 dark:border bg-white backdrop-blur-xl rounded-3xl transition-all duration-300 z-50 dark:border-slate-900/65 dark:bg-slate-950/50 dark:shadow-[0_20px_50px_rgba(0,0,0,0.5)]',
            isExpanded ? 'w-64' : 'w-20'
        )}>



            {/* Navigation Items */}
            <div className="flex-1 overflow-y-auto overflow-x-visible py-6 flex flex-col gap-8 px-3 no-scrollbar">
                {visibleGroups.map((group, gIdx) => {
                    // Group collapse state is shared between the labelled and the icon-only
                    // sidebar, so collapsing a group hides its icons in both modes.
                    const isGroupCollapsed = collapsedGroups.has(group.id);
                    return (
                    <div key={group.id ?? gIdx} className="flex flex-col gap-1">
                        {/* Group Header with Stable Vertical Footprint */}
                        {group.title && (
                            isExpanded ? (
                                <button
                                    type="button"
                                    onClick={() => toggleGroup(group.id)}
                                    aria-expanded={!isGroupCollapsed}
                                    aria-controls={`nav-group-${group.id}`}
                                    className="group/header flex items-center gap-1.5 text-[10px] text-slate-600 uppercase tracking-widest px-3 mb-2.5 font-bold h-4 w-full hover:text-slate-950 dark:text-slate-500 dark:hover:text-slate-300 transition-colors cursor-pointer"
                                >
                                    <ChevronDown className={cn('w-3 h-3 shrink-0 transition-transform duration-200', isGroupCollapsed && '-rotate-90')} />
                                    <span className="truncate">{group.title}</span>
                                </button>
                            ) : (
                                <button
                                    type="button"
                                    onClick={() => toggleGroup(group.id)}
                                    aria-expanded={!isGroupCollapsed}
                                    aria-controls={`nav-group-${group.id}`}
                                    aria-label={`${isGroupCollapsed ? 'Expand' : 'Collapse'} ${group.title}`}
                                    title={group.title}
                                    className="group/header relative mb-2.5 flex h-4 items-center justify-center px-3 text-slate-500 hover:text-slate-900 dark:text-slate-600 dark:hover:text-slate-300 transition-colors cursor-pointer"
                                >
                                    {gIdx > 0 ? <div className="absolute left-[10px] w-[36px] h-[1px] bg-slate-200 dark:bg-slate-800/80 shrink-0" /> : null}
                                    <ChevronDown className={cn('relative w-3 h-3 shrink-0 transition-transform duration-200 bg-white dark:bg-slate-950 rounded-full', isGroupCollapsed && '-rotate-90')} />
                                </button>
                            )
                        )}

                        <div id={`nav-group-${group.id}`} className={cn('flex flex-col gap-1', isGroupCollapsed && 'hidden')}>
                        {group.items.map((item) => {
                            const Icon = MENU_ICONS[item.icon];
                            const isActive = currentView === item.view;

                            return (
                                <React.Fragment key={item.id}>
                                    {item.separator && (
                                        <div className="my-2 border-t border-slate-900/60 mx-3" />
                                    )}
                                    <button
                                        onClick={() => handleItemClick(item.view, item.disabled)}
                                        aria-disabled={item.disabled}
                                        title={!isExpanded ? item.label : undefined}
                                        className={cn(
                                            'group relative flex items-center gap-4 px-3 py-2.5 rounded-2xl transition-all duration-300 w-full text-left font-normal border',
                                            isActive && 'lens-navigation-active',
                                            isActive
                                                ? (ITEM_THEMES[item.view]?.activeBg || 'bg-gradient-to-r from-cyan-50 via-blue-50/70 to-white border-cyan-300/60 text-cyan-800 shadow-[inset_0_1px_1px_rgba(255,255,255,0.7)] dark:from-cyan-950/20 dark:via-blue-950/10 dark:to-slate-950/20 dark:border-cyan-500/20 dark:text-cyan-300 dark:shadow-[inset_0_1px_1px_rgba(255,255,255,0.02)]')
                                                : 'border-transparent text-slate-700 hover:bg-slate-100 hover:text-slate-950 dark:text-slate-400 dark:hover:bg-slate-900/30 dark:hover:text-white cursor-pointer'
                                        )}
                                    >
                                        {/* Active Side Indicator */}
                                        {isActive && (
                                            <div className={cn('lens-navigation-indicator absolute left-1 top-3.5 bottom-3.5 w-1 rounded-full', ITEM_THEMES[item.view]?.indicator || 'bg-gradient-to-b from-cyan-400 to-blue-500')} />
                                        )}

                                        <div className={cn(
                                            'lens-navigation-icon p-1.5 rounded-xl transition-all duration-300',
                                            isActive
                                                ? (ITEM_THEMES[item.view]?.activeIcon || 'bg-gradient-to-br from-cyan-500 to-blue-500 text-white shadow-[0_0_15px_rgba(6,182,212,0.35)]')
                                                : cn('bg-transparent group-hover:brightness-125', item.iconColor || 'text-slate-400')
                                        )}>
                                            <Icon className="w-5 h-5 shrink-0" />
                                        </div>

                                        {isExpanded && (
                                            <div className="flex flex-1 items-center justify-between truncate">
                                                <span className="flex items-center gap-1.5 min-w-0">
                                                    <span className={cn('text-sm tracking-wide truncate', isActive ? 'lens-navigation-active-label font-medium' : 'font-normal')}>
                                                        {item.label}
                                                    </span>
                                                    {item.badge && (
                                                        <span className="nav-exp-badge text-[9px] text-amber-300 font-mono px-1.5 py-0.5 rounded bg-amber-950/40 border border-amber-800/50 shrink-0 tracking-wider">
                                                            {item.badge}
                                                        </span>
                                                    )}
                                                </span>

                                                {item.disabled && (
                                                    <span className="text-[9px] text-slate-500 font-mono px-2 py-0.5 rounded bg-slate-950 border border-slate-900/80 shrink-0 tracking-wider">
                                                        Coming soon
                                                    </span>
                                                )}
                                            </div>
                                        )}

                                        {/* Tooltip when Collapsed */}
                                        {!isExpanded && (
                                            <div className="absolute left-16 top-1/2 -translate-y-1/2 px-3 py-1.5 bg-white border border-slate-200/80 text-slate-900 dark:bg-slate-900 dark:border-slate-800/60 dark:text-white text-xs font-medium rounded-lg invisible group-hover:visible shadow-xl z-[99999] whitespace-nowrap flex items-center gap-2">
                                                {item.label}
                                                {item.badge && <span className="text-[10px] text-amber-300 font-mono">({item.badge})</span>}
                                                {item.disabled && <span className="text-[10px] text-slate-500 font-mono">(Coming soon)</span>}
                                            </div>
                                        )}
                                    </button>
                                </React.Fragment>
                            );
                        })}
                        </div>
                    </div>
                    );
                })}
            </div>

            {principal && (
                <div className={cn(
                    'relative border-t border-slate-200/80 py-3 flex items-center gap-2 text-xs text-slate-700 dark:border-slate-900/65 dark:text-slate-400 shrink-0',
                    isExpanded ? 'px-4 justify-between' : 'px-3 justify-center'
                )}>
                    {accountMenuOpen && (
                        <div className="fixed inset-0 z-40" onClick={() => setAccountMenuOpen(false)} aria-hidden="true" />
                    )}
                    <button
                        type="button"
                        onClick={() => setAccountMenuOpen((open) => !open)}
                        className="relative z-50 flex min-w-0 items-center gap-2"
                        title={principal.username}
                        aria-haspopup="menu"
                        aria-expanded={accountMenuOpen}
                    >
                        <span className="sidebar-account-avatar flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-slate-300 bg-slate-100 text-sm font-semibold uppercase text-slate-900 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100">
                            {(principal.username || '?').slice(0, 1)}
                        </span>
                        {isExpanded && <span className="truncate text-sm text-slate-800 dark:text-slate-200">{principal.username}</span>}
                    </button>
                    {isExpanded && (
                        <button
                            type="button"
                            onClick={() => setAccountMenuOpen((open) => !open)}
                            className="relative z-50 rounded-lg p-1.5 text-slate-600 transition-colors hover:bg-slate-100 hover:text-slate-950 dark:text-slate-400 dark:hover:bg-slate-900/50 dark:hover:text-white"
                            aria-label="Account menu"
                            aria-haspopup="menu"
                            aria-expanded={accountMenuOpen}
                        >
                            <Menu size={16} />
                        </button>
                    )}
                    {accountMenuOpen && (
                        <div
                            role="menu"
                            className={cn(
                                'absolute bottom-full z-50 mb-2 w-44 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-2xl dark:border-slate-800 dark:bg-slate-900',
                                isExpanded ? 'right-4' : 'left-3'
                            )}
                        >
                            <button
                                type="button"
                                role="menuitem"
                                onClick={() => { setAccountMenuOpen(false); setShowChangePassword(true); }}
                                className="block w-full px-3 py-2 text-left text-xs text-slate-800 transition-colors hover:bg-slate-100 hover:text-slate-950 dark:text-slate-300 dark:hover:bg-slate-800 dark:hover:text-white"
                            >
                                Change password
                            </button>
                            <button
                                type="button"
                                role="menuitem"
                                onClick={() => { setAccountMenuOpen(false); logout(); }}
                                className="block w-full border-t border-slate-200 px-3 py-2 text-left text-xs text-rose-600 transition-colors hover:bg-slate-100 dark:border-slate-800 dark:text-rose-300 dark:hover:bg-slate-800"
                            >
                                Sign out
                            </button>
                        </div>
                    )}
                </div>
            )}

            {/* Fixed Bottom-Left Toggle */}
            <div className={cn(
                'lens-navigation-footer mt-auto flex shrink-0 items-center gap-1 border-t border-slate-200/80 bg-slate-50/60 py-4 dark:border-slate-900/65 dark:bg-slate-950/20 rounded-b-3xl',
                isExpanded ? 'justify-between px-4' : 'justify-center px-3'
            )}>
                <Button
                    variant="ghost"
                    size="icon"
                    onClick={onToggleTheme}
                    aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
                    title={theme === 'dark' ? 'Light mode' : 'Dark mode'}
                >
                    {theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}
                </Button>
                <button
                    onClick={() => setIsExpanded(!isExpanded)}
                    className="h-8 w-8 rounded-xl font-mono text-slate-600 hover:text-slate-950 hover:bg-slate-100 dark:hover:text-white dark:hover:bg-slate-900/50 transition-all flex items-center justify-center cursor-pointer text-xs font-bold border border-slate-200 dark:border-slate-900/60 dark:hover:border-slate-800/65"
                    title={isExpanded ? "Collapse Sidebar" : "Expand Sidebar"}
                >
                    {isExpanded ? "<|" : "|>"}
                </button>
            </div>
        </aside>
        {showChangePassword && (
            <ChangePasswordModal
                onClose={() => setShowChangePassword(false)}
                onSaved={() => setShowChangePassword(false)}
            />
        )}
        </>
    );
}
