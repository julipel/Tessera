import { LoginForm } from "@/components/admin/LoginForm";

export default function AdminLogin() {
  const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  return <LoginForm apiUrl={apiUrl} />;
}
