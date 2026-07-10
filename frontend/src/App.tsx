// Root app: providers, router, and the auth gate. When locked, the entire app
// is replaced by the passcode screen. Owner-only routes are guarded.

import { lazy, Suspense, type ReactElement } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { AuthProvider, useAuth } from './hooks/useAuth';
import { ThemeProvider } from './hooks/useTheme';
import { Layout } from './components/Layout';
import { LockScreen } from './routes/LockScreen';
import { Library } from './routes/Library';
import { Spinner } from './components/ui';
import { Toaster } from './components/Toast';

// Reader and owner views are code-split: the library is the common entry, and
// the reader pulls in its heavier view logic only when actually reading.
const Reader = lazy(() =>
  import('./routes/Reader').then((m) => ({ default: m.Reader })),
);
const Detail = lazy(() =>
  import('./routes/Detail').then((m) => ({ default: m.Detail })),
);
const Downloads = lazy(() =>
  import('./routes/Downloads').then((m) => ({ default: m.Downloads })),
);
const Settings = lazy(() =>
  import('./routes/Settings').then((m) => ({ default: m.Settings })),
);

function OwnerRoute({ children }: { children: ReactElement }) {
  const { isOwner } = useAuth();
  return isOwner ? children : <Navigate to="/" replace />;
}

function AuthedApp() {
  const { unlocked } = useAuth();
  if (!unlocked) return <LockScreen />;

  return (
    <Suspense fallback={<Spinner />}>
      <Routes>
        {/* Reader is full-bleed — rendered outside the chrome Layout. */}
        <Route path="/read/:id" element={<Reader />} />

        <Route element={<Layout />}>
          <Route path="/" element={<Library />} />
          <Route path="/archive/:id" element={<Detail />} />
          <Route
            path="/downloads"
            element={
              <OwnerRoute>
                <Downloads />
              </OwnerRoute>
            }
          />
          <Route
            path="/settings"
            element={
              <OwnerRoute>
                <Settings />
              </OwnerRoute>
            }
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </Suspense>
  );
}

export default function App() {
  return (
    <ThemeProvider>
      <BrowserRouter>
        <AuthProvider>
          <AuthedApp />
          <Toaster />
        </AuthProvider>
      </BrowserRouter>
    </ThemeProvider>
  );
}
