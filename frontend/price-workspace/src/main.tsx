import { render } from 'preact';
import { onlineApi } from './api';
import { App } from './App';
import { createDemoApi } from './demo';
import './style.css';

const demo = location.protocol === 'file:' || new URLSearchParams(location.search).get('demo') === '1';
render(<App api={demo ? createDemoApi() : onlineApi} demo={demo} />, document.getElementById('app')!);
