1 dim a(26):a(20)=15:a(21)=51:a(22)=39:a(23)=54:a(24)=99:a(25)=113:a(26)=116
2 n=0
3 a(n)=0:n=n+1:if n<20 then 3
4 s=0:l=0:timer t:t=t and 7:e=0:g=0:gosub 150:goto 20
20 h=1:gosub 110:cls:print "TETRIS":n=0
21 q=a(n):print "|";:o=0
22 print q mod 2;:q=q/2:o=o+1:if o<10 then 22
23 print "|":n=n+1:if n<20 then 21
24 print "S ";:print s:print "L ";:print l
25 print "a d w s space q":h=-1:gosub 110:goto 30
30 wait 2:key k:g=g+1
31 if k=113 then 99
32 if k=97 then 50
33 if k=100 then 50
34 if k=119 then 55
35 if k=115 then 65
36 if k=32 then 70
37 if g<5 then 30
38 g=0:gosub 140
39 if e=1 then 90
40 goto 20
50 z=(k-97)/3*2-1:x=x+z:h=0:gosub 110
51 if f=0 then 80
52 x=x-z:goto 80
55 m=p
56 if t=0 then 59
57 if t=1 then 80
58 gosub 130:goto 60
59 p=4384-p
60 h=0:gosub 110
61 if f=0 then 80
62 p=m:goto 80
65 g=0:gosub 140
66 if e=1 then 90
67 goto 20
70 gosub 140
71 if b=0 then 70
72 if e=1 then 90
73 g=0:goto 20
80 if g<5 then 20
81 goto 38
90 cls:print "GAME OVER":print "S ";:print s:print "L ";:print l
91 print "r/q"
92 wait 2:key k
93 if k=114 then 2
94 if k=113 then 99
95 goto 92
99 end
110 f=0:v=0:q=p
111 u=q mod 16:d=y+v
112 if u=0 then 123
113 if x<0 then 129
114 c=u:i=x
115 if i=0 then 117
116 c=c*2:i=i-1:goto 115
117 if c>1023 then 129
118 if d>19 then 129
119 if h=0 then 121
120 a(d)=a(d)+c*h:goto 123
121 if (a(d) and c)>0 then 129
123 q=q/16:v=v+1:if v<4 then 111
124 return
129 f=1:return
130 q=0:i=0:r=p
131 if r mod 2=0 then 137
132 u=i mod 4:v=i/4:d=u*4+2-v:c=1
133 if d=0 then 136
134 c=c*2:d=d-1:goto 133
136 q=q+c
137 r=r/2:i=i+1:if i<12 then 131
138 p=q:return
140 b=0:y=y+1:h=0:gosub 110
141 if f=0 then 149
142 y=y-1:b=1:h=1:gosub 110:gosub 160:gosub 150
149 return
150 t=(t+1) mod 7:p=a(20+t):x=3:y=0:h=0:gosub 110
151 if f=0 then 153
152 e=1
153 return
160 v=19
161 if a(v)=1023 then 165
162 v=v-1:if v>-1 then 161
163 return
165 d=v
166 if d=0 then 169
167 a(d)=a(d-1):d=d-1:goto 166
169 a(0)=0:l=l+1:s=s+100:goto 161
