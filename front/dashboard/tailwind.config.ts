import type { Config } from "tailwindcss";
import typography from "@tailwindcss/typography";

export default <Partial<Config>>{
  theme: {
    extend: {
      colors: {
        galadril: {
          DEFAULT: "#f59e0b",
          hover: "#d97706",
        },
      },
    },
  },
  plugins: [typography],
};
