import './styles/tokens.css';
import './styles/base.css';
import './styles/layout.css';
import { createClient } from './api/index';
import { fatal, startApp } from './shell/app';

const root = document.getElementById('app');
if (root) {
  startApp({ root, makeClient: createClient }).catch((error: unknown) => {
    console.error(error);
    fatal(root, 'PulseBridge could not start. Reload the page to try again.');
  });
}
