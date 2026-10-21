import React from 'react';
import { cn } from '../lib/cn';

// Lens Assistant bot icon: the eyes blink and the icon hops using the shared
// prism-bot-* animations in the application styles.
export function AnimatedBotIcon({ className }) {
    return (
        <svg
            className={cn('prism-bot-icon', className)}
            xmlns="http://www.w3.org/2000/svg"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
        >
            <path d="M12 8V4H8" />
            <rect width="16" height="12" x="4" y="8" rx="2" />
            <path d="M2 14h2" />
            <path d="M20 14h2" />
            <g className="prism-bot-eyes">
                <path d="M15 13v2" />
                <path d="M9 13v2" />
            </g>
        </svg>
    );
}
