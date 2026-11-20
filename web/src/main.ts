import './styles/tokens.css';
import './styles/base.css';
import './styles/layout.css';
import { createClient } from './api/index';
import { createBotCheck } from './bot-check/index';
import { fatal, startApp } from './shell/app';

const root = document.getElementById('app');
if (root) {
  createBotCheck()
    .then((botCheck) => startApp({ root, makeClient: createClient, botCheck }))
    .catch((error: unknown) => {
      console.error(error);
      fatal(root, 'PulseBridge could not start. Reload the page to try again.');
    });
}
