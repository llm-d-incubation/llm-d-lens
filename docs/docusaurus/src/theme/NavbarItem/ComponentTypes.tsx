import ComponentTypes from '@theme-original/NavbarItem/ComponentTypes';
import NavbarGitHubStar from '@site/src/components/NavbarGitHubStar';

export default {
  ...ComponentTypes,
  // Lets docusaurus.config.ts navbar items use `type: 'custom-githubStar'`
  // for the GitHub logo + live star count item (see
  // src/components/NavbarGitHubStar).
  'custom-githubStar': NavbarGitHubStar,
};
