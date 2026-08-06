import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './app/App';
import { RouterProvider } from './app/router';
import { ThemeProvider } from './shared/theme/ThemeProvider';
import '@xyflow/react/dist/style.css';
import './shared/styles/tokens.css';
import './shared/styles/base.css';
import './shared/styles/components.css';
import './shared/styles/layout.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ThemeProvider>
      <RouterProvider>
        <App />
      </RouterProvider>
    </ThemeProvider>
  </React.StrictMode>,
);
