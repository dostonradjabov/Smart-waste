function refresh(){

fetch("/data")

.then(r=>r.json())

.then(data=>{

// quti 1
document.getElementById("distance1").innerText=data.bin1.distance
document.getElementById("fill1").innerText=data.bin1.fill
document.getElementById("bar1").style.width=data.bin1.fill+"%"

// quti 2
document.getElementById("distance2").innerText=data.bin2.distance
document.getElementById("fill2").innerText=data.bin2.fill
document.getElementById("bar2").style.width=data.bin2.fill+"%"

})

}

setInterval(refresh,2000)