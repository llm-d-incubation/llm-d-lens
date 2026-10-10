import React, { type ReactNode } from 'react';
import Link from '@docusaurus/Link';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import {
  faBoltLightning,
  faBookOpen,
  faChartLine,
  faCircleQuestion,
  faFlask,
  faGaugeHigh,
  faLifeRing,
  faRocket,
  faRoute,
  faServer,
  faShieldHalved,
  faSitemap,
  type IconDefinition,
} from '@fortawesome/free-solid-svg-icons';
import styles from './styles.module.css';

/**
 * Minimal re-implementation of Fern's <Card>/<CardGroup> MDX components so
 * migrated docs pages keep working unchanged. `icon` is a Fern/FontAwesome
 * icon name (kebab-case, matching FontAwesome's icon ids); it's looked up in
 * ICONS and rendered as a FontAwesome icon. Unknown names render nothing.
 */
const ICONS: Record<string, IconDefinition> = {
  'bolt-lightning': faBoltLightning,
  'book-open': faBookOpen,
  'chart-line': faChartLine,
  'circle-question': faCircleQuestion,
  flask: faFlask,
  'gauge-high': faGaugeHigh,
  'life-ring': faLifeRing,
  rocket: faRocket,
  route: faRoute,
  server: faServer,
  // Fern/Font Awesome Pro's "shield-check" has no free-solid equivalent;
  // "shield-halved" is the closest available free icon.
  'shield-check': faShieldHalved,
  sitemap: faSitemap,
};

export function CardGroup({
  cols = 2,
  children,
}: {
  cols?: number;
  children: ReactNode;
}): JSX.Element {
  return (
    <div
      className={styles.cardGroup}
      style={{ ['--card-group-cols' as string]: cols }}
    >
      {children}
    </div>
  );
}

export function Card({
  title,
  href,
  icon,
  children,
}: {
  title: string;
  href?: string;
  icon?: string;
  children?: ReactNode;
}): JSX.Element {
  const iconDefinition = icon ? ICONS[icon] : undefined;
  const content = (
    <>
      <div className={styles.cardTitle}>
        {iconDefinition && (
          <FontAwesomeIcon icon={iconDefinition} className={styles.cardIcon} />
        )}
        {title}
      </div>
      {children && <div className={styles.cardBody}>{children}</div>}
    </>
  );
  return href ? (
    <Link to={href} className={styles.card}>
      {content}
    </Link>
  ) : (
    <div className={styles.card}>{content}</div>
  );
}
