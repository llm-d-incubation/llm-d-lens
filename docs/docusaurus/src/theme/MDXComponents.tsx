import React from 'react';
// Default implementation, that you can customize
import MDXComponents from '@theme-original/MDXComponents';
import Tabs from '@theme/Tabs';
import TabItem from '@theme/TabItem';
import { Card, CardGroup } from '@site/src/components/Card';

export default {
  ...MDXComponents,
  Tabs,
  TabItem,
  Card,
  CardGroup,
};
