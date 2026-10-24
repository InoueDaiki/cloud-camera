import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { MantineProvider, localStorageColorSchemeManager } from '@mantine/core';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import '@mantine/core/styles.css';
import '@mantine/dates/styles.css';
import 'dayjs/locale/ja';
import { DatesProvider } from '@mantine/dates';
import { theme, cssVariablesResolver } from './theme';
import { App } from './App';
import './global.css';

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

const colorSchemeManager = localStorageColorSchemeManager({ key: 'cc-color-scheme' });

async function main() {
  if (import.meta.env.VITE_MOCK === 'true') {
    const { startMock } = await import('./mocks/start');
    await startMock();
  }
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <MantineProvider
        theme={theme}
        cssVariablesResolver={cssVariablesResolver}
        defaultColorScheme="auto"
        colorSchemeManager={colorSchemeManager}
      >
        <DatesProvider settings={{ locale: 'ja', firstDayOfWeek: 0 }}>
          <QueryClientProvider client={queryClient}>
            <App />
          </QueryClientProvider>
        </DatesProvider>
      </MantineProvider>
    </StrictMode>,
  );
}

void main();
