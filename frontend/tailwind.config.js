/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: {
          950: '#0A0A09',
          900: '#111110',
          850: '#14130D',
          800: '#17160F',
          750: '#1D1B14',
          700: '#242118',
          600: '#2E2B22',
          500: '#3A362B',
        },
        cream: {
          50: '#F8F5EE',
          100: '#F4EFE4',
          200: '#E8E0CF',
          300: '#D4C9B0',
          400: '#B8AB8E',
          500: '#9A8E72',
        },
        amber: {
          400: '#F2C875',
          500: '#E8B85B',
          600: '#D4A23F',
          700: '#A87E2E',
        },
        signal: {
          matched: '#5FB98B',
          matchedDim: '#3A6B52',
          review: '#E8B85B',
          reviewDim: '#7A6433',
          exception: '#D9614E',
          exceptionDim: '#7A3A30',
          processing: '#7A8FA8',
          processingDim: '#3E4A5C',
        },
      },
      fontFamily: {
        sans: ['Inter', 'Geist', 'Manrope', 'system-ui', 'sans-serif'],
        serif: ['"Instrument Serif"', 'Georgia', 'serif'],
        mono: ['"JetBrains Mono"', '"SF Mono"', 'Menlo', 'monospace'],
      },
      fontSize: {
        'display': ['clamp(3rem, 8vw, 7rem)', { lineHeight: '0.95', letterSpacing: '-0.03em' }],
        'hero': ['clamp(2.5rem, 6vw, 5rem)', { lineHeight: '1.0', letterSpacing: '-0.025em' }],
      },
      borderRadius: {
        'xl2': '18px',
      },
      boxShadow: {
        'panel': '0 1px 0 0 rgba(255,255,255,0.03) inset, 0 0 0 1px rgba(232,184,91,0.06)',
        'glow': '0 0 24px -4px rgba(232,184,91,0.25)',
        'glow-strong': '0 0 40px -8px rgba(232,184,91,0.4)',
      },
      keyframes: {
        'fade-in': {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        'fade-up': {
          '0%': { opacity: '0', transform: 'translateY(12px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        'pulse-soft': {
          '0%, 100%': { opacity: '0.4' },
          '50%': { opacity: '1' },
        },
        'scan': {
          '0%': { transform: 'translateY(-100%)' },
          '100%': { transform: 'translateY(100%)' },
        },
        'shimmer': {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
      },
      animation: {
        'fade-in': 'fade-in 0.4s ease-out',
        'fade-up': 'fade-up 0.5s ease-out',
        'pulse-soft': 'pulse-soft 2s ease-in-out infinite',
        'scan': 'scan 1.2s ease-in-out',
        'shimmer': 'shimmer 1.5s ease-in-out infinite',
      },
      transitionTimingFunction: {
        'smooth': 'cubic-bezier(0.22, 1, 0.36, 1)',
      },
    },
  },
  plugins: [],
};
