// モックモード(npm run dev:mock)でだけ読み込む
import { setupWorker } from 'msw/browser';
import { handlers } from './handlers';

export async function startMock() {
  const worker = setupWorker(...handlers);
  await worker.start({ onUnhandledFrame: 'bypass', quiet: true });
}
