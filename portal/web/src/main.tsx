import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import { Check, Info, TriangleAlert, X } from "lucide-react";
import { Toaster } from "sonner";
import { App } from "./App";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
        <Toaster
          closeButton
          position="bottom-right"
          icons={{
            success: <Check className="h-4 w-4 text-[#7EE2A8]" />,
            error: <X className="h-4 w-4 text-[#FF9B8F]" />,
            warning: <TriangleAlert className="h-4 w-4 text-[#FFD27A]" />,
            info: <Info className="h-4 w-4 text-white" />,
          }}
          toastOptions={{
            style: {
              background: "#15171A",
              color: "#FFFFFF",
              border: "none",
              borderRadius: 12,
              boxShadow: "0 12px 32px rgba(20,22,26,0.24)",
              fontFamily: "inherit",
              fontSize: 14,
            },
          }}
        />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
