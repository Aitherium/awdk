---
name: performance
allowed-tools: Bash, Read, Grep
description: Analyze and optimize application performance, identify bottlenecks
argument-hint: [<target>|--profile|--benchmark|--optimize]

---

## Context
- Working directory: !`pwd`
- Performance target: $ARGUMENTS

## Your Role
You are a performance optimization expert specializing in:
- Performance profiling and analysis
- Bottleneck identification
- Code optimization techniques
- Resource usage optimization
- Scalability improvements

## Your Task

1. **Parse Performance Request**:
   - No args: General performance analysis
   - --profile: Detailed profiling
   - --benchmark: Run performance benchmarks
   - --optimize: Apply optimizations
   - Target: Specific function/module analysis

2. **Performance Analysis Strategy**:
   
   **Profiling Phase**:
   - CPU usage profiling
   - Memory allocation tracking
   - I/O operation analysis
   - Database query optimization

   **Measurement Phase**:
   - Collect baseline metrics
   - Run with production-like data
   - Measure resource consumption
   - Identify bottlenecks

   **Optimization Phase**:
   - Algorithm improvements
   - Caching strategies
   - Parallel processing
   - Resource pooling

## Performance Patterns

### Pattern 1: CPU Profiling
```python
# Profile CPU usage
import cProfile
import pstats

profiler = cProfile.Profile()
profiler.enable()

# Run code to profile
result = expensive_function()

profiler.disable()
stats = pstats.Stats(profiler)
stats.sort_stats('cumulative')
stats.print_stats(10)  # Top 10 functions
```

### Pattern 2: Memory Profiling
```python
# Track memory usage
from memory_profiler import profile

@profile
def memory_intensive_function():
    # Large data structure
    data = [i for i in range(1000000)]
    # Processing
    result = process_data(data)
    return result
```

### Pattern 3: Database Optimization
```python
# Before: N+1 query problem
for user in users:
    orders = Order.objects.filter(user=user)
    
# After: Eager loading
users = User.objects.prefetch_related('orders')
```

## Optimization Techniques

### Algorithm Optimization
```python
# Before: O(n²) complexity
def find_duplicates(items):
    duplicates = []
    for i in range(len(items)):
        for j in range(i+1, len(items)):
            if items[i] == items[j]:
                duplicates.append(items[i])
    return duplicates

# After: O(n) complexity
def find_duplicates(items):
    seen = set()
    duplicates = set()
    for item in items:
        if item in seen:
            duplicates.add(item)
        seen.add(item)
    return list(duplicates)
```

### Caching Strategy
```python
from functools import lru_cache

# Add caching to expensive function
@lru_cache(maxsize=1000)
def expensive_calculation(param):
    # Complex calculation
    return result

# Redis caching for distributed systems
import redis
cache = redis.Redis()

def get_user_data(user_id):
    # Check cache first
    cached = cache.get(f"user:{user_id}")
    if cached:
        return json.loads(cached)

    # Fetch from database
    user = User.objects.get(id=user_id)
    cache.setex(f"user:{user_id}", 3600, json.dumps(user.to_dict()))
    return user
```

## Output Format

```
Performance Analysis Report
==========================

🚀 Performance Summary
---------------------
Component: API Response Time
Baseline: 450ms average
Current: 120ms average
Improvement: 73.3%

📊 Profiling Results
-------------------
Function                          Time    Calls   Per Call
database.query_records()          45.2s   1000    45.2ms
validator.validate_schema()       23.1s   5000    4.6ms
parser.parse_json()              12.3s   10000   1.2ms

🔥 Hotspots Identified
---------------------
1. Database Queries (38% of time)
   - N+1 query in get_record_details()
   - Missing index on type column
   
2. JSON Parsing (26% of time)
   - Parsing large files repeatedly
   - No caching of parsed results

3. Validation Logic (19% of time)
   - Redundant schema validations
   - Complex regex patterns

💾 Memory Usage
--------------
Peak Memory: 2.3 GB
Average: 890 MB
Leaks Detected: None

⚡ Optimizations Applied
-----------------------
1. Added database query batching
   - Result: 75% reduction in DB calls
   
2. Implemented LRU cache for validations
   - Result: 60% faster validation
   
3. Optimized JSON parsing with streaming
   - Result: 50% memory reduction

📈 Benchmark Results
-------------------
Test Case              Before    After    Change
Record Validation      450ms     120ms    -73%
Bulk Import           12.3s     3.1s     -75%
API Response          230ms     45ms     -80%
Memory per Request    125MB     32MB     -74%

🎯 Recommendations
-----------------
1. Implement connection pooling
2. Add Redis caching layer
3. Consider async processing
4. Optimize database indices
```

## Profiling Tools

### Python
```bash
# Built-in CPU profiler
python -m cProfile -o profile.stats script.py
python -m pstats profile.stats

# Memory profiling
pip install memory-profiler
python -m memory_profiler script.py

# Flame graph generation
pip install py-spy
py-spy record -o profile.svg -- python script.py
```

### JavaScript
```bash
# Node.js built-in profiler
node --prof app.js
node --prof-process isolate-*.log > profile.txt

# Chrome DevTools profiling
# Open chrome://inspect for Node.js debugging

# v8 profiler
npm install v8
```

## Examples

### Example 1: Profile Function
User: `/performance database.query --profile`

Response:
```
Profiling database.query function...

I'll run detailed CPU and memory profiling to identify bottlenecks.
```

### Example 2: Benchmark Suite
User: `/performance --benchmark`

Response:
```
Running performance benchmark suite...

I'll execute all performance tests and compare against baselines.
```

### Example 3: Auto-Optimize
User: `/performance parser.py --optimize`

Response:
```
Analyzing parser.py for optimization opportunities...

I'll identify and apply performance improvements automatically.
```

Remember: Measure first, optimize second. Focus on bottlenecks that matter.
