/** @type {import('tailwindcss').Config} */
export default {
	content: ['./src/**/*.{html,js,svelte,ts}'],
	theme: {
		extend: {
			colors: {
				// The app's palette: blue-black panels, soft ink levels (named for the strategy
				// container, where the theme started).
				sc: {
					bg: '#07080a',
					panel: '#0c0e11',
					panel2: '#11141a',
					raise: '#181c23',
					hover: '#0f1217',
					line: '#1c2026',
					line2: '#2a2f38',
					ink: '#eef1f5',
					ink2: '#aab1bc',
					ink3: '#747c88',
					ink4: '#4b525c',
				},
			},
			fontFamily: {
				// The app's type: IBM Plex, bundled via @fontsource (imported in the root layout).
				sans: ['"IBM Plex Sans"', 'ui-sans-serif', 'system-ui', '-apple-system', '"Segoe UI"', 'Roboto', 'sans-serif'],
				mono: ['"IBM Plex Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
				plex: ['"IBM Plex Sans"', 'ui-sans-serif', 'system-ui', '-apple-system', '"Segoe UI"', 'Roboto', 'sans-serif'],
				'plex-cond': ['"IBM Plex Sans Condensed"', '"IBM Plex Sans"', 'ui-sans-serif', 'system-ui', 'sans-serif'],
				'plex-mono': ['"IBM Plex Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
			},
		},
	},
	plugins: [],
};
