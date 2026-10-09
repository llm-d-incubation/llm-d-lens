import React from 'react';
import styles from './styles.module.css';

/**
 * Reserves the vertical space the live Star/Watch/Fork row used to occupy
 * on the homepage. The row itself was removed (its counts frequently
 * couldn't be fetched from networks that block the browser from reaching
 * api.github.com directly, leaving permanent "—" placeholders), but the
 * surrounding layout still expects this gap, so this keeps the same
 * margin/height instead of letting the page reflow.
 */
export default function GitHubStats(): JSX.Element {
  return <div className={styles.statsRow} aria-hidden="true" />;
}
