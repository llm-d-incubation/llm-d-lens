import { createPortal } from 'react-dom';
import { CircleAlert, Info, X } from 'lucide-react';
import { cn } from '../lib/cn';
import { Button } from './Button';
import { Panel } from './Panel';

export function Toast({ message, title = 'Notification', tone = 'info', onDismiss }) {
    if (!message || typeof document === 'undefined') return null;
    const danger = tone === 'danger';
    const Icon = danger ? CircleAlert : Info;

    return createPortal(
        <div className="pointer-events-none fixed bottom-6 right-4 z-[150] w-[calc(100%-2rem)] max-w-sm sm:right-6">
            <Panel padding="sm" className="pointer-events-auto flex items-start gap-3 shadow-xl">
                <span className={cn(
                    'flex h-8 w-8 shrink-0 items-center justify-center rounded-full',
                    danger
                        ? 'bg-rose-100 text-rose-700 dark:bg-rose-500/15 dark:text-rose-400'
                        : 'bg-sky-100 text-sky-700 dark:bg-sky-500/15 dark:text-sky-400',
                )}>
                    <Icon size={17} aria-hidden="true" />
                </span>
                <div role={danger ? 'alert' : 'status'} aria-atomic="true" className="min-w-0 flex-1">
                    <p className="text-sm font-semibold text-theme-text">{title}</p>
                    <p className="mt-1 max-h-48 overflow-y-auto whitespace-pre-wrap break-words text-xs leading-relaxed text-theme-muted">
                        {message}
                    </p>
                </div>
                <Button type="button" variant="ghost" size="icon" className="-mr-1 -mt-1 shrink-0"
                    aria-label="Dismiss notification" onClick={onDismiss}>
                    <X size={14} aria-hidden="true" />
                </Button>
            </Panel>
        </div>,
        document.body,
    );
}
