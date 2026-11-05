# Your documents go here

This folder is where you drop the files the app reads about you. Nothing in it is ever added to git except this note, so your resume and career notes stay on your own computer.

## What to drop here

1. **Your base resume as a `.docx` file.** This is the one resume every tailored version starts from. The app keeps its layout, header and fonts exactly as they are and only rewrites the summary, the skills lines and the bullet points. Name it anything you like, ideally with "resume" or "CV" in the name (for example `Alex_Example_Resume.docx`).
2. **Extra career documents** as `.md`, `.txt` or `.docx` files. Write as much detail as you want about what you actually did: projects, systems you built, numbers, tools, team size, outcomes. For example `career_2023_2026.md`. The more real detail there is, the more the tailored resume has to work with.

Keep one resume `.docx` here. If there are several, the app uses the one whose name contains "resume" or "cv" (alphabetically first) and tells you which one it picked. Any other `.docx` that does not look like a resume counts as an extra document.

## How the app uses these files

- **Base resume.** On the Profile page the app shows the resume it found here. Press the button to make it your approved base resume. The app copies it into its private folder and records its fingerprint. If you later replace the file, the Profile page shows that the folder copy differs and lets you adopt the new one. It never swaps your approved resume behind your back.
- **Extra documents.** Their text is read every time the app checks how well a job fits you and every time it tailors your resume. It is treated as evidence you wrote yourself: a rewritten bullet may use a fact from these notes, but every bullet still comes from a line on your base resume, and the app never invents experience, tools or numbers that appear in neither place.
- **Size.** Up to about 12,000 characters of extra text are read (roughly four pages). Files are read in alphabetical order, so name the most important one so it sorts first (`01_career_2023_2026.md`). Longer text is cut at the limit and the app notes that it was.

## Good to know

- Only files directly in this folder are read. Hidden files and this README are skipped.
- If the app runs on another machine (the NAS), you cannot reach its folder directly. Use "Add a resume or career notes" on the Profile page instead: it takes `.docx`, `.md` or `.txt` files up to 5 MB and puts them in that machine's folder.
- You can point the app at a different folder with `AMORE_CANDIDATE_DIR` and change the text limit with `AMORE_CANDIDATE_SUPPLEMENTAL_MAX_CHARS` in `.env`.
- Do not put passwords, ID numbers or anything you would not want in a resume in these files. They are sent to the AI model when it tailors a resume or ranks a job.
