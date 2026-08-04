import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './app/App';
import { ConfirmProvider } from './components/common/ConfirmDialog';
import { ToastProvider } from './components/common/Toast';
import { RouterProvider } from './lib/router';
// React Flow's stylesheet must come first so our theme rules win the cascade.
import '@xyflow/react/dist/style.css';
import './styles.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <RouterProvider>
      <ToastProvider>
        <ConfirmProvider>
          <App />
        </ConfirmProvider>
      </ToastProvider>
    </RouterProvider>
  </React.StrictMode>,
);
