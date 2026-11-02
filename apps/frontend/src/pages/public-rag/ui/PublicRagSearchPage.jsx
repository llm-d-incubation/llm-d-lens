import React, { useEffect, useRef, useState } from 'react';
import { Database } from 'lucide-react';
import { AsyncState, Button, EmptyState, Input, Label, ModuleHeader, Panel, Textarea } from '../../../shared/ui';
import { FormError } from '../../../shared/ui/FormError.jsx';
import { createLatestResourceRequest } from '../../../shared/lib/resourceLoader.js';
import { publicRagSearchRequest, publicRagSourceUrl, searchPublicRagDocuments } from './publicRagBackend.js';

export function PublicRagSearchResults({ data }) {
    if (!data) return <EmptyState title="Search published documents" message="Enter a question and select Search. Only approved public sources are searched." />;
    return (
        <div className="space-y-4">
            {data.index_stale && (
                <Panel role="status" title="Search index is stale">
                    <p className="text-sm text-theme-muted">Outdated sources have been excluded. Rebuild the index to include their updated content.</p>
                    <p className="mt-2 break-words text-xs text-theme-muted">Affected origins: {data.affected_origins?.join(', ') || 'Not reported'}</p>
                </Panel>
            )}
            <p className="break-words text-xs text-theme-muted">
                Lexical retrieval · Build: {data.retrieval_build_id || 'Not reported'} · Candidates: {data.candidate_count} · Excluded: {data.excluded_count}
                {data.truncated && ' · Results truncated; refine your question or increase the limit.'}
            </p>
            <p className="text-xs text-theme-muted">Scores indicate text relevance, not hardware compatibility or measured performance.</p>
            {!data.results.length && <EmptyState title="No matching documents" message="Try different terms or remove the origin filter." />}
            {data.results.map((result) => {
                const citation = result.citation || {};
                const sourceUrl = publicRagSourceUrl(citation);
                return (
                    <Panel key={result.evidence_id} title={result.title || 'Untitled document'}>
                        <p className="mb-2 text-xs text-theme-muted">{result.heading_path?.join(' / ')}</p>
                        <pre className="whitespace-pre-wrap break-words font-sans text-sm leading-relaxed text-theme-text">{result.text}</pre>
                        <details className="mt-4 text-xs text-theme-muted">
                            <summary className="cursor-pointer font-semibold text-theme-text">Citation and retrieval details</summary>
                            <dl className="mt-3 space-y-2 break-words">
                                {[
                                    ['Origin', citation.origin_id],
                                    ['Source path', citation.source_path],
                                    ['Revision', citation.source_revision],
                                    ['SHA256', citation.sha256],
                                    ['Artifact reference', citation.artifact_uri],
                                    ['Source pointer', typeof citation.source_pointer === 'object' ? JSON.stringify(citation.source_pointer) : citation.source_pointer],
                                    ['Citation headings', citation.heading_path?.join(' / ')],
                                    ['Published manifest', citation.published_manifest_id],
                                    ['Evidence ID', result.evidence_id],
                                    ['Chunk ID', result.chunk_id],
                                    ['Matched branches', result.matched_branches?.join(', ')],
                                    ['Lexical rank', result.lexical_rank],
                                    ['Score', result.score],
                                ].map(([label, value]) => (
                                    <div key={label}><dt className="font-semibold">{label}</dt><dd>{value ?? 'Not reported'}</dd></div>
                                ))}
                            </dl>
                            {sourceUrl && <a href={sourceUrl} target="_blank" rel="noopener noreferrer" className="mt-3 inline-block underline text-theme-text">View public source</a>}
                        </details>
                    </Panel>
                );
            })}
        </div>
    );
}

export default function PublicRagSearchPage({ onToggleMobileNav }) {
    const [question, setQuestion] = useState('');
    const [origins, setOrigins] = useState('');
    const [limit, setLimit] = useState('10');
    const [state, setState] = useState({ loading: false, data: null, error: null });
    const [validationError, setValidationError] = useState('');
    const [requests] = useState(createLatestResourceRequest);
    const lastQuery = useRef(null);
    useEffect(() => () => requests.cancel(), [requests]);

    function search(query) {
        lastQuery.current = query;
        setValidationError('');
        setState({ loading: true, data: null, error: null });
        void requests.run(signal => searchPublicRagDocuments(query, { signal }), {
            onSuccess: data => setState({ loading: false, data, error: null }),
            onError: error => setState({ loading: false, data: null, error }),
        });
    }

    function submit(event) {
        event.preventDefault();
        try {
            search(publicRagSearchRequest({ question, origins, limit }));
        } catch (error) {
            setValidationError(error.message);
        }
    }

    return (
        <div className="space-y-6 p-4 sm:p-6">
            <ModuleHeader icon={Database} title="Document Search" description="Search published public documents using lexical retrieval." onToggleMobileNav={onToggleMobileNav} />
            <Panel>
                <form onSubmit={submit} className="space-y-4">
                    <div>
                        <Label htmlFor="public-rag-question">Question</Label>
                        <Textarea id="public-rag-question" value={question} onChange={event => setQuestion(event.target.value)} required rows={3} placeholder="How does prefix caching work?" />
                    </div>
                    <div className="grid gap-4 sm:grid-cols-[1fr_8rem]">
                        <div>
                            <Label htmlFor="public-rag-origins">Origins (optional)</Label>
                            <Input id="public-rag-origins" value={origins} onChange={event => setOrigins(event.target.value)} aria-describedby="public-rag-origin-help" placeholder="https://github.com/llm-d/llm-d" />
                            <p id="public-rag-origin-help" className="mt-1 text-xs text-theme-muted">Comma-separated approved origin IDs. Leave blank to search all approved origins.</p>
                        </div>
                        <div>
                            <Label htmlFor="public-rag-limit">Result limit</Label>
                            <Input id="public-rag-limit" type="number" min="1" max="50" step="1" required value={limit} onChange={event => setLimit(event.target.value)} />
                        </div>
                    </div>
                    <FormError message={validationError} />
                    <div className="flex gap-2">
                        <Button type="submit" disabled={!question.trim()}>Search</Button>
                        {state.loading && <Button type="button" variant="secondary" onClick={() => {
                            requests.cancel();
                            setState({ loading: false, data: null, error: null });
                        }}>Cancel</Button>}
                    </div>
                </form>
            </Panel>
            <section aria-label="Document search results" aria-live="polite" aria-busy={state.loading}>
                <AsyncState loading={state.loading} error={state.error} onRetry={() => search(lastQuery.current)}>
                    <PublicRagSearchResults data={state.data} />
                </AsyncState>
            </section>
        </div>
    );
}
