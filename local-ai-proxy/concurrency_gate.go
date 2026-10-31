package main

import "sync"

// userModelGate is the running count and FIFO wait queue for one
// (user, model) pair.
type userModelGate struct {
	running int
	waiters []chan struct{}
}

func (l *UserModelLimiter) releaser(k userModelKey, g *userModelGate) func() {
	var once sync.Once
	return func() { once.Do(func() { l.releaseSlot(k, g) }) }
}

// releaseSlot frees one running slot, promoting the oldest waiter if
// there is one. The slot is handed over rather than closed and reopened,
// so the running count only changes when the queue is empty.
func (l *UserModelLimiter) releaseSlot(k userModelKey, g *userModelGate) {
	l.mu.Lock()
	if len(g.waiters) > 0 {
		w := g.waiters[0]
		g.waiters = g.waiters[1:]
		l.mu.Unlock()
		close(w)
		l.observeQueue(k.model, -1)
		return
	}
	g.running--
	l.dropGateIfIdle(k, g)
	l.mu.Unlock()
}

// dropGateIfIdle removes the gate once nothing is running or waiting,
// so the map doesn't retain one entry per user/model pair that ever
// made a request. Callers must hold l.mu.
func (l *UserModelLimiter) dropGateIfIdle(k userModelKey, g *userModelGate) {
	if g.running == 0 && len(g.waiters) == 0 {
		delete(l.gates, k)
	}
}

func (l *UserModelLimiter) observeQueue(model string, delta int) {
	if l.observer == nil {
		return
	}
	if delta > 0 {
		l.observer.QueueInc(model)
	} else {
		l.observer.QueueDec(model)
	}
}

func removeWaiter(g *userModelGate, ch chan struct{}) bool {
	for i, w := range g.waiters {
		if w == ch {
			g.waiters = append(g.waiters[:i], g.waiters[i+1:]...)
			return true
		}
	}
	return false
}

// InFlight reports how many requests user currently has running
// against model.
func (l *UserModelLimiter) InFlight(user, model string) int {
	if l == nil {
		return 0
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if g := l.gates[userModelKey{user: user, model: model}]; g != nil {
		return g.running
	}
	return 0
}

// Queued reports how many requests user currently has waiting for a
// slot against model.
func (l *UserModelLimiter) Queued(user, model string) int {
	if l == nil {
		return 0
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if g := l.gates[userModelKey{user: user, model: model}]; g != nil {
		return len(g.waiters)
	}
	return 0
}
