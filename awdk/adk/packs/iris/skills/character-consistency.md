# Character Consistency Skill

Keeping a character on-model across a set. This skill exists because the intuitive
approach does not work, and confidently shipping the intuitive approach wastes
hours and produces a cast of siblings instead of one person.

## THE CORE LAW

**txt2img + IPAdapter CANNOT pin a character.**

An IPAdapter reference nudges the *distribution* toward your reference image. It
gives you a family resemblance — same vibe, same hair colour, same general face
shape — and a different person in every frame. No amount of prompt engineering,
weight tuning, or seed locking converts that into identity.

Consistency comes from exactly three techniques. Pick one and say which:

| technique | when | what it pins |
|---|---|---|
| **i2v / animate** | motion, expressions, short clips | animates ONE approved still — identity is inherited, not re-rolled |
| **inpaint** | change one region, keep the rest | freezes the frame; only the masked region is regenerated |
| **character LoRA** | many novel poses/angles from scratch | trains identity INTO the weights — the only route to true from-scratch generation |

## The decision

Ask one question: **does the new image need a pose/angle the base still does not
have?**

- **No** → the base still already contains the answer. Use **inpaint** (change
  clothing, expression, background) or **i2v** (add motion). Cheap, fast, exact.
- **Yes** → you need a **LoRA**. There is no shortcut. Budget the dataset and the
  training run, or renegotiate the ask down to poses inpaint/i2v can cover.

## The workflow that works

### 1. Establish and FREEZE a base still

Generate until you have one image that is genuinely the character. Then stop
generating and record it:

```python
memory_write({
    "role": "fact",
    "content": "Character <name> base still: <path>",
    "metadata": {"seed": 12345, "checkpoint": "...", "prompt": "...", "cfg": 6.5},
})
```

The seed and checkpoint matter as much as the file. A base still you cannot
reproduce is a base still you will lose.

### 2. Derive everything else FROM that still

Never re-roll the character from text once the base exists. Every subsequent
asset is a derivation: inpaint a region, or animate the whole thing.

### 3. Only then, if the ask demands novel poses, train a LoRA

The base still plus its derivations become the seed of the dataset. A LoRA needs
coverage — multiple angles and lighting conditions — which is exactly what the
i2v/inpaint derivations produce.

## What to say to the user

When someone asks for "the same character in ten different poses", do not accept
the task as stated and quietly deliver ten cousins. Say:

> Ten novel poses from scratch means a character LoRA — txt2img with a reference
> image will give a family resemblance, not the same person. I can either (a)
> train a LoRA, or (b) cover the poses the base still supports via inpaint/i2v.
> Which do you want?

That is a two-sentence conversation that saves a wasted afternoon.

## Failure signatures

| symptom | cause |
|---|---|
| "close but the face keeps changing" | txt2img+IPAdapter — the core law. Switch technique. |
| identity drifts as the clip goes on | i2v run too long; shorten and chain from re-anchored frames |
| inpaint seam / different skin tone | mask too tight — feather it, and match the denoise to the region |
| LoRA overfits to one pose | dataset lacks angle coverage; add i2v-derived frames |

## Discipline

- Record the seed, checkpoint, and prompt for every approved base still.
- Never promise from-scratch consistency without a LoRA.
- Look at the output before declaring a match — `vision` exists for this. Two
  images can both be 200 OK and only one is the character.
